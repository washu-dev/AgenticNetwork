"""call_endpoint — the generic "reach the wrapped external service" system tool.

This is the single capability of an LLM-endpoint-wrapper agent: the brain decides
*what* to ask the external service, and this tool performs the actual call. The
endpoint URL, HTTP method, protocol, and auth are **bound at construction** (from
``EndpointConfig``); the model only supplies the natural-language ``request`` and
an optional structured ``payload``. The auth token is read from an environment
variable at call time and is NEVER exposed to the model or written to disk.

Two protocols:
  * ``http`` — POST a JSON body ``{"input": request, **payload}`` to the endpoint
    (works for any plain REST/JSON service).
  * ``a2a`` — wrap ``request`` in an A2A JSON-RPC ``message/send`` envelope (for
    external services that already speak the network's agent-to-agent protocol),
    mirroring ``registration_service/handlers/api_proxy_handler.py``.

Intentionally **dependency-free** (stdlib ``urllib`` only): a generated agent can
run this without httpx / a2a-sdk installed, and it is unit-testable without a
network. For streaming A2A, an agent can graduate to the a2a-sdk client; for the
request/response wrapper case this is sufficient.
"""
from __future__ import annotations

import copy
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

# Cap how much of an upstream body we ingest so a huge/never-ending response
# cannot exhaust memory (the socket timeout bounds each read, not total size).
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000

# Don't follow redirects: a 3xx on a POST would otherwise be silently re-issued as
# a body-less GET. With this opener a 3xx surfaces as an HTTPError instead, matching
# the httpx references (follow_redirects=False + raise_for_status).
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)

# Mask obvious secret-shaped substrings if a misbehaving upstream echoes them in
# an error body (the body is surfaced to the model, hence into the trace).
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(bearer)\s+\S+"), r"\1 <redacted>"),
    # Basic auth (base64 user:pass) — same shape as Bearer. Digest/AWS4-HMAC and
    # other schemes are not exhaustively covered; this is best-effort masking of
    # secret-shaped substrings an upstream might echo, not a guarantee.
    (re.compile(r"(?i)(basic)\s+\S+"), r"\1 <redacted>"),
    (re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)\S+"), r"\1<redacted>"),
    (re.compile(r"(?i)(token\s*[:=]\s*)\S+"), r"\1<redacted>"),
]


def _redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


@dataclass
class EndpointConfig:
    """Where + how to reach the wrapped external service. Bound, not model-chosen."""

    url: str
    method: str = "POST"
    protocol: str = "http"          # 'http' (plain JSON) | 'a2a' (JSON-RPC message/send)
    auth_env: str | None = None     # NAME of the env var holding the token (never the token)
    auth_scheme: str = "Bearer"     # 'Bearer' (=> "Bearer <token>") | 'raw' (verbatim header)
    timeout: float = 120.0
    content_type: str = "application/json"
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    # For a multi-endpoint "manager" agent: a human label + what this endpoint does
    # and its I/O. `name` seeds the per-endpoint tool name; `description` is baked
    # into that tool's schema description so the model routes to the right one.
    # Unused by the single-endpoint wrapper (it has exactly one `call_endpoint`).
    name: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        scheme = urlsplit(self.url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"EndpointConfig.url must be http(s); got scheme {scheme!r} in {self.url!r}."
            )


# Tool names the Chat Completions API accepts: ^[a-zA-Z0-9_-]{1,64}$.
_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def endpoint_tool_name(label: str, index: int) -> str:
    """Derive a valid, readable tool name for one endpoint of a manager agent.

    `label` (the endpoint's ``name``) is slugified into ``call_<slug>``; an empty
    label falls back to ``call_endpoint_<index>``. Uniqueness across endpoints is
    the caller's job (the multi-endpoint spec builder dedupes)."""
    slug = _TOOL_NAME_RE.sub("_", (label or "").strip().lower()).strip("_")
    base = f"call_{slug}" if slug else f"call_endpoint_{index}"
    return base[:64]


# The schema is fixed (one source of truth); make_call_endpoint returns a copy so
# callers can mutate (e.g. retitle the description per agent) without aliasing.
CALL_ENDPOINT_SCHEMA_TEMPLATE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "call_endpoint",
        "description": (
            "Call the external service this agent wraps. Put the user's request as "
            "natural language in `request`; optionally add structured fields in "
            "`payload` if the endpoint expects them. Returns the endpoint's response."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "The request to send to the external service, in natural language.",
                },
                "payload": {
                    "type": "object",
                    "description": "Optional structured fields to send to the endpoint.",
                    "additionalProperties": True,
                },
            },
            "required": ["request"],
            "additionalProperties": False,
        },
    },
}


def _auth_headers(cfg: EndpointConfig) -> dict[str, str]:
    if not cfg.auth_env:
        return {}
    token = os.getenv(cfg.auth_env)
    if not token:
        return {}
    if cfg.auth_scheme.lower() == "raw":
        return {"Authorization": token}
    return {"Authorization": f"{cfg.auth_scheme} {token}"}


def _a2a_envelope(request: str) -> dict[str, Any]:
    # Fresh ids per call: a conformant A2A peer keys messages/tasks by these, so
    # reusing them would collapse refine-loop or concurrent calls. (Matches the
    # planner/api_proxy references, which never reuse an id across requests.)
    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": request}],
                "messageId": uuid.uuid4().hex,
            }
        },
    }


def _extract_a2a_text(result: Any) -> str:
    """Best-effort human-readable string from an A2A JSON-RPC response."""
    if not isinstance(result, dict):
        return str(result)
    body = result.get("result", result)
    for path in (("status", "message", "parts"), ("artifact", "parts"), ("parts",)):
        node: Any = body
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, list):
            texts = [p.get("text", "") for p in node if isinstance(p, dict)]
            joined = " ".join(t for t in texts if t)
            if joined:
                return joined
    return json.dumps(body)


def make_call_endpoint(
    cfg: EndpointConfig,
    *,
    tool_name: str = "call_endpoint",
    description: str | None = None,
) -> tuple[dict[str, Any], Callable[..., dict[str, Any]]]:
    """Return ``(schema, fn)`` for a call_endpoint tool bound to ``cfg``.

    The returned fn has signature ``call_endpoint(*, request, payload=None)`` —
    its keyword params match the schema properties, so validate_tool_registry
    accepts it without the **kwargs escape hatch.

    ``tool_name`` / ``description`` override the schema's function name and
    description. They default to the single-endpoint wrapper's values (so existing
    callers are unchanged); a multi-endpoint manager passes a distinct name +
    per-endpoint description so the model can route among several endpoints.
    """

    def call_endpoint(*, request: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if cfg.protocol == "a2a":
            body: dict[str, Any] = _a2a_envelope(request)
        else:
            body = {"input": request}
            if payload:
                body.update(payload)

        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": cfg.content_type, **_auth_headers(cfg)}
        req = urllib.request.Request(cfg.url, data=data, headers=headers, method=cfg.method)
        cap = cfg.max_response_bytes

        try:
            # _OPENER does not follow redirects, so a 3xx raises HTTPError below.
            with _OPENER.open(req, timeout=cfg.timeout) as resp:
                raw_bytes = resp.read(cap + 1)
                status = getattr(resp, "status", None) or resp.getcode()
        except urllib.error.HTTPError as exc:
            detail = exc.read(cap).decode("utf-8", errors="replace") if exc.fp else ""
            return {
                "ok": False,
                "status_code": exc.code,
                "error": f"HTTP {exc.code}",
                # Upstream's own error body (not this agent's token); secret-shaped
                # substrings are masked before it reaches the model/trace.
                "response_text": _redact(detail),
            }
        except Exception as exc:  # network error, timeout, bad URL, ...
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        truncated = len(raw_bytes) > cap
        raw = raw_bytes[:cap].decode("utf-8", errors="replace")

        if truncated:
            # A truncated body won't parse as JSON; return the raw prefix instead.
            parsed: Any = raw
        else:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw

        out: dict[str, Any] = {"ok": True, "status_code": status, "response": parsed}
        if truncated:
            out["truncated"] = True
        if cfg.protocol == "a2a":
            out["text"] = _extract_a2a_text(parsed if isinstance(parsed, dict) else {"text": raw})
        return out

    # Deep copy so a caller mutating the returned schema (e.g. retitling the
    # description) cannot alias the shared template's nested dicts.
    schema = copy.deepcopy(CALL_ENDPOINT_SCHEMA_TEMPLATE)
    schema["function"]["name"] = tool_name
    if description is not None:
        schema["function"]["description"] = description
    return schema, call_endpoint
