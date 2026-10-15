"""Dev identity proxy: identity replacement, faithful bodies, unbuffered SSE."""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from devstack import dev_identity_proxy


class Upstream(BaseHTTPRequestHandler):
    """Echoes what it received; /sse sends three events 0.4 s apart."""

    def _echo(self) -> None:
        if self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()  # no Content-Length: the body ends when the connection closes
            for i in range(3):
                self.wfile.write(f"event: trace\ndata: {i}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.4)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body_sha256": hashlib.sha256(body).hexdigest(),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _echo

    def log_message(self, *args) -> None:
        pass


def _serve(server: ThreadingHTTPServer) -> int:
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server.server_address[1]


@pytest.fixture
def proxy_port():
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    up_port = _serve(upstream)
    proxy = dev_identity_proxy.make_server("127.0.0.1", 0, f"http://127.0.0.1:{up_port}", "dev@example.test")
    yield _serve(proxy)
    proxy.shutdown()
    upstream.shutdown()
    proxy.server_close()
    upstream.server_close()


def request(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    return response, conn


def test_client_identity_is_replaced_by_the_dev_user(proxy_port):
    spoofed = {"mail": "evil@example.test", "EPPN": "evil", "Remote-User": "evil", "remote_user": "evil", "X-Other": "kept"}
    response, conn = request(proxy_port, "GET", "/me?x=1", headers=spoofed)
    seen = json.loads(response.read())
    conn.close()
    assert seen["path"] == "/me?x=1"
    assert seen["headers"]["mail"] == "dev@example.test"
    assert not {"eppn", "remote-user", "remote_user"} & set(seen["headers"])
    assert seen["headers"]["x-other"] == "kept"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_methods_and_binary_multipart_bodies_pass_through(proxy_port, method):
    boundary = "devstackboundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nhello\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.bin\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + bytes(range(256)) * 64 + f"\r\n--{boundary}--\r\n".encode()
    content_type = f"multipart/form-data; boundary={boundary}"
    response, conn = request(proxy_port, method, "/messages", body=body, headers={"Content-Type": content_type})
    seen = json.loads(response.read())
    conn.close()
    assert seen["method"] == method
    assert seen["headers"]["content-type"] == content_type
    assert seen["body_sha256"] == hashlib.sha256(body).hexdigest()


def test_sse_is_streamed_as_it_arrives(proxy_port):
    started = time.monotonic()
    response, conn = request(proxy_port, "GET", "/sse")
    first = response.read1(65536)
    first_at = time.monotonic() - started
    rest = response.read()
    total = time.monotonic() - started
    conn.close()
    assert first.startswith(b"event: trace\ndata: 0")
    assert first_at < 0.35 < total, (first_at, total)  # the first event did not wait for the rest
    assert (first + rest).count(b"event: trace") == 3


@pytest.mark.parametrize("host", ["attacker.example:8080", "192.168.1.20:8080", "localhost.attacker.example"])
def test_non_loopback_host_is_refused(proxy_port, host):
    response, conn = request(proxy_port, "GET", "/me", headers={"Host": host})
    body = json.loads(response.read())
    conn.close()
    assert response.status == 403
    assert "localhost" in body["detail"]


@pytest.mark.parametrize("host", ["localhost:8080", "127.0.0.1:8080", "[::1]:8080", "app.localhost"])
def test_loopback_hosts_are_proxied(proxy_port, host):
    response, conn = request(proxy_port, "GET", "/me", headers={"Host": host})
    seen = json.loads(response.read())
    conn.close()
    assert response.status == 200
    assert seen["headers"]["mail"] == "dev@example.test"


def test_unreachable_planner_is_a_502():
    proxy = dev_identity_proxy.make_server("127.0.0.1", 0, "http://127.0.0.1:9", "dev@example.test")
    port = _serve(proxy)
    try:
        response, conn = request(port, "GET", "/health")
        assert response.status == 502 and "unreachable" in json.loads(response.read())["detail"]
        conn.close()
    finally:
        proxy.shutdown()
        proxy.server_close()
