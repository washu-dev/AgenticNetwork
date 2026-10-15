"""Local stand-in for the SSO front door: a reverse proxy that signs you in.

In production an Apache/Shibboleth service provider authenticates the user and
passes their identity to the planner in the ``mail`` / ``eppn`` / ``remote-user``
headers. On a laptop this proxy plays that role: it drops any identity headers
the client sent (so nobody can claim another identity through it) and injects
``mail: <DEV_USER_EMAIL>`` (default ``dev@example.test``). It answers only requests
addressed to a loopback name (``localhost``, ``*.localhost``, ``127.0.0.1``, ``[::1]``),
so a web page that rebinds its own DNS name to this port cannot drive it.

It forwards every method and body unchanged (JSON, multipart uploads) and
streams responses through as they arrive, so the chat UI's SSE stream from
``/messages/stream`` is not buffered.

Run: ``python3 -m devstack.dev_identity_proxy --port 8080 --upstream
http://127.0.0.1:8090``. Standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

DEFAULT_USER = "dev@example.test"
IDENTITY_HEADERS = frozenset({"mail", "eppn", "remote-user"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def loopback_host(host_header: str | None) -> bool:
    """True when the request's Host names this machine's loopback (or is absent)."""
    if not host_header:
        return True
    host = (urlsplit(f"//{host_header}").hostname or "").lower()
    return host in LOOPBACK_HOSTS or host.endswith(".localhost")
# Hop-by-hop headers (RFC 9110 section 7.6.1) are never forwarded.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
UPSTREAM_TIMEOUT_SECONDS = 600.0
_CHUNK = 64 * 1024


def is_identity_header(name: str) -> bool:
    # Also catch underscore spellings (remote_user), which some servers map to
    # the hyphenated name.
    return name.strip().lower().replace("_", "-") in IDENTITY_HEADERS


def forward_request_headers(headers: Any, *, user: str, upstream_host: str, client_ip: str) -> list[tuple[str, str]]:
    """Headers to send upstream: client headers minus identity and hop-by-hop
    headers, plus the injected identity."""
    connection_tokens = {t.strip().lower() for t in (headers.get("Connection") or "").split(",") if t.strip()}
    out: list[tuple[str, str]] = []
    for name, value in headers.items():
        lowered = name.lower()
        if is_identity_header(lowered) or lowered in HOP_BY_HOP or lowered in connection_tokens:
            continue
        if lowered in ("host", "content-length", "expect"):
            continue
        out.append((name, value))
    previous = headers.get("X-Forwarded-For")
    out.append(("X-Forwarded-For", f"{previous}, {client_ip}" if previous else client_ip))
    out.append(("Host", upstream_host))
    out.append(("mail", user))
    return out


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "devstack-identity-proxy/1"
    protocol_version = "HTTP/1.1"

    # Set by make_server().
    upstream_host = "127.0.0.1"
    upstream_port = 8090
    user = DEFAULT_USER

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        self._proxy()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = do_GET

    def _read_body(self) -> bytes | None:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            body = bytearray()
            while True:
                size_line = self.rfile.readline()
                size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass  # trailers
                    return bytes(body)
                body += self.rfile.read(size)
                self.rfile.readline()
        length = self.headers.get("Content-Length")
        if length is None:
            return None
        return self.rfile.read(int(length))

    def _proxy(self) -> None:
        if not loopback_host(self.headers.get("Host")):
            self.close_connection = True
            self._error(403, "the dev identity proxy only answers requests to localhost")
            return
        try:
            body = self._read_body()
        except (ValueError, OSError):
            self._error(400, "malformed request body")
            return
        upstream_host_header = f"{self.upstream_host}:{self.upstream_port}"
        headers = forward_request_headers(
            self.headers, user=self.user, upstream_host=upstream_host_header, client_ip=self.client_address[0]
        )
        conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=UPSTREAM_TIMEOUT_SECONDS)
        try:
            conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                conn.putheader(name, value)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body if body else None)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            self._error(502, f"planner unreachable at {upstream_host_header}: {exc}")
            return
        try:
            self._relay(response)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True  # client went away
        finally:
            conn.close()

    def _relay(self, response: http.client.HTTPResponse) -> None:
        self.send_response_only(response.status, response.reason)
        length = response.getheader("Content-Length")
        no_body = self.command == "HEAD" or response.status in (204, 304) or 100 <= response.status < 200
        connection_tokens = {
            t.strip().lower() for t in (response.getheader("Connection") or "").split(",") if t.strip()
        }
        for name, value in response.getheaders():
            lowered = name.lower()
            if lowered in HOP_BY_HOP or lowered in connection_tokens or lowered == "content-length":
                continue
            self.send_header(name, value)
        chunked = not no_body and length is None
        if no_body:
            if length is not None:
                self.send_header("Content-Length", length)
        elif chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", length)
        self.end_headers()
        if no_body:
            return
        while True:
            # read1 returns as soon as some bytes arrive, so SSE events pass
            # through one by one instead of waiting for a full buffer.
            data = response.read1(_CHUNK)
            if not data:
                break
            if chunked:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            else:
                self.wfile.write(data)
            self.wfile.flush()
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    def _error(self, status: int, message: str) -> None:
        data = json.dumps({"detail": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} identity-proxy {fmt % args}\n")


def make_server(host: str, port: int, upstream: str, user: str) -> ThreadingHTTPServer:
    parts = urlsplit(upstream if "//" in upstream else f"http://{upstream}")
    if parts.scheme != "http":
        raise ValueError(f"only http:// upstreams are supported, got {upstream!r}")
    handler = type(
        "BoundProxyHandler",
        (ProxyHandler,),
        {"upstream_host": parts.hostname or "127.0.0.1", "upstream_port": parts.port or 80, "user": user},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dev identity proxy: injects a fixed signed-in user.")
    parser.add_argument("--host", default="127.0.0.1", help="Listen address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--upstream", default="http://127.0.0.1:8090", help="Planner base URL.")
    parser.add_argument("--user", default=os.getenv("DEV_USER_EMAIL") or DEFAULT_USER)
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port, args.upstream, args.user)
    print(
        f"identity proxy on http://{args.host}:{server.server_address[1]} -> {args.upstream} as {args.user}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
