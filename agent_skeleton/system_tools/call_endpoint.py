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
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit

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


def _issue_request(
    cfg: EndpointConfig,
    *,
    url: str,
    method: str,
    headers: dict[str, str],
    data: bytes | None,
) -> dict[str, Any]:
    """Issue one HTTP request and shape the response into the tool-result dict.

    Shared by ``make_call_endpoint`` (the generic prose tool) and
    ``make_operation_tool`` (typed per-operation tools) so redirect handling,
    secret redaction, size-capping, and error shaping have ONE implementation.
    Returns ``{ok, status_code, response[, truncated]}`` on success,
    ``{ok: False, status_code, error, response_text}`` on an HTTP error, or
    ``{ok: False, error}`` on any other failure. Never raises."""
    cap = cfg.max_response_bytes
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
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
    return out


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
        out = _issue_request(cfg, url=cfg.url, method=cfg.method, headers=headers, data=data)
        if out.get("ok") and cfg.protocol == "a2a":
            parsed = out["response"]
            out["text"] = _extract_a2a_text(parsed if isinstance(parsed, dict) else {"text": str(parsed)})
        return out

    # Deep copy so a caller mutating the returned schema (e.g. retitling the
    # description) cannot alias the shared template's nested dicts.
    schema = copy.deepcopy(CALL_ENDPOINT_SCHEMA_TEMPLATE)
    schema["function"]["name"] = tool_name
    if description is not None:
        schema["function"]["description"] = description
    return schema, call_endpoint


# --- Typed per-operation tools (one Chat Completions function tool per API call) ---
#
# The generic call_endpoint above gives the model ONE free-text tool. For an API
# with a known set of operations (GET /weather, POST /reports, ...) we instead
# expose each operation as its OWN typed tool: the schema's `parameters` ARE the
# operation's real arguments, so the model emits structured, validated calls and
# the fn builds the concrete method + path + query + body. Same (schema, fn) shape
# and same `_issue_request` core as call_endpoint — just finer-grained and typed.

@dataclass
class OperationConfig:
    """One API operation, exposed to the model as a single typed tool.

    ``params`` is a JSON-Schema ``properties`` dict (e.g. ``{"city": {"type":
    "string"}}``); ``required`` lists the required property names. Where each
    argument goes is decided per-name: a name that appears as ``{name}`` in
    ``path`` is a PATH parameter; otherwise ``param_in`` maps it to ``"query"``,
    ``"body"``, or ``"header"`` — defaulting to ``"query"`` for GET/DELETE/HEAD and
    ``"body"`` for POST/PUT/PATCH. The base URL/auth/timeout live in the shared
    ``EndpointConfig`` passed alongside."""

    name: str
    method: str = "GET"
    path: str = "/"
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)      # JSON-Schema properties
    required: list[str] = field(default_factory=list)
    param_in: dict[str, str] = field(default_factory=dict)    # name -> query|body|header (path auto-detected)


def operation_tool_name(label: str, index: int) -> str:
    """Slugify an operation name into a valid Chat Completions tool name
    (``^[a-zA-Z0-9_-]{1,64}$``); an empty label falls back to ``op_<index>``."""
    slug = _TOOL_NAME_RE.sub("_", (label or "").strip()).strip("_")
    return (slug or f"op_{index}")[:64]


# Minimal, stdlib-only value validation (the engine stays dependency-free — no
# jsonschema / pydantic). Checks required presence, rejects unexpected args, and
# does a shallow JSON-type check; anything invalid comes back as an error the model
# can correct on the next loop step, exactly like any other tool result.
_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _validate_args(
    props: dict[str, Any], required: list[str], args: dict[str, Any]
) -> list[str]:
    """Return a list of problems (empty list == valid)."""
    problems: list[str] = []
    for name in required:
        if args.get(name) is None:
            problems.append(f"missing required argument '{name}'")
    for name, value in args.items():
        if name not in props:
            problems.append(f"unexpected argument '{name}'")
            continue
        if value is None:
            continue
        want = props[name].get("type")
        expected = _JSON_TYPES.get(want)
        if expected is None:
            continue
        # bool is a subclass of int, so guard integer/number against True/False.
        if want in ("integer", "number") and isinstance(value, bool):
            problems.append(f"argument '{name}' must be {want}")
        elif not isinstance(value, expected):
            problems.append(f"argument '{name}' must be {want}")
    return problems


def _join_url(base_url: str, path: str) -> str:
    if not path:
        return base_url
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _default_param_location(method: str) -> str:
    return "query" if method.upper() in ("GET", "DELETE", "HEAD") else "body"


def make_operation_tool(
    base: EndpointConfig,
    operation: OperationConfig,
    *,
    index: int = 0,
) -> tuple[dict[str, Any], Callable[..., dict[str, Any]]]:
    """Return ``(schema, fn)`` for ONE typed operation tool bound to ``base``.

    The schema's ``parameters`` are the operation's typed ``params``; ``fn`` takes
    those as keyword args, validates them, partitions each into path/query/body/
    header, builds the concrete request, and issues it via the shared
    ``_issue_request``. ``fn`` uses ``**kwargs`` so it opts out of the static
    schema<->signature check in ``validate_tool_registry`` — the runtime
    ``_validate_args`` check (stronger, value-level) covers it instead."""
    props = operation.params or {}
    required = list(operation.required or [])
    tool_name = operation_tool_name(operation.name, index)

    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": operation.description
            or f"{operation.method.upper()} {operation.path}",
            "parameters": {
                "type": "object",
                "properties": copy.deepcopy(props),
                "required": required,
                "additionalProperties": False,
            },
        },
    }

    def operation_call(**kwargs: Any) -> dict[str, Any]:
        problems = _validate_args(props, required, kwargs)
        if problems:
            return {"ok": False, "error": "invalid arguments: " + "; ".join(problems)}

        path = operation.path
        query: dict[str, Any] = {}
        body: dict[str, Any] = {}
        extra_headers: dict[str, str] = {}
        for pname, pval in kwargs.items():
            if "{" + pname + "}" in operation.path:  # path parameter
                path = path.replace("{" + pname + "}", quote(str(pval), safe=""))
                continue
            loc = operation.param_in.get(pname) or _default_param_location(operation.method)
            if loc == "query":
                query[pname] = pval
            elif loc == "header":
                extra_headers[pname] = str(pval)
            else:
                body[pname] = pval

        url = _join_url(base.url, path)
        if query:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(query, doseq=True)}"

        method = operation.method.upper()
        headers = {**_auth_headers(base), **extra_headers}
        if method in ("POST", "PUT", "PATCH"):
            data: bytes | None = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", base.content_type)
        else:
            data = None

        return _issue_request(base, url=url, method=method, headers=headers, data=data)

    return schema, operation_call
