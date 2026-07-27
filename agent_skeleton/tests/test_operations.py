"""Tests for typed per-operation wrapper tools (Feature B — "each API call = a tool").

Each API operation becomes its own Chat Completions function tool with a real typed
argument schema; the tool fn builds the concrete method + path + query + body and
issues it. Stdlib-only; the only network is a localhost stub (mirrors test_manager).
Run:
    python -m pytest agent_skeleton/tests/test_operations.py -q
    python -m agent_skeleton.tests.test_operations
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent_skeleton.spec import AgentSpec, operation_wrapper_spec
from agent_skeleton.system_tools.call_endpoint import (
    EndpointConfig,
    OperationConfig,
    make_operation_tool,
    operation_tool_name,
)

# Each hit records what the stub server actually received.
_HITS: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, body):
        _HITS.append({"method": self.command, "path": self.path, "body": body})
        resp = json.dumps({"ok": True, "path": self.path, "method": self.command}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_GET(self):
        self._reply(None)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else ""
        self._reply(json.loads(raw) if raw else {})


class _Server:
    def __enter__(self):
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def __exit__(self, *a):
        self.srv.shutdown()


def test_operation_tool_name_slugs_and_falls_back():
    assert operation_tool_name("get_weather", 0) == "get_weather"
    assert operation_tool_name("Get Weather!", 1) == "Get_Weather"
    assert operation_tool_name("", 3) == "op_3"
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", operation_tool_name("x" * 200, 0))


def test_make_operation_tool_builds_typed_schema():
    base = EndpointConfig(url="https://api.example/v1")
    schema, _fn = make_operation_tool(
        base,
        OperationConfig(
            name="get_weather",
            method="GET",
            path="/weather",
            description="current weather",
            params={"city": {"type": "string"}, "units": {"type": "string"}},
            required=["city"],
        ),
    )
    fn = schema["function"]
    assert fn["name"] == "get_weather"
    assert fn["description"] == "current weather"
    # The schema's parameters ARE the operation's typed args (not the generic
    # request/payload) — this is the whole point of Feature B.
    assert set(fn["parameters"]["properties"]) == {"city", "units"}
    assert fn["parameters"]["required"] == ["city"]
    assert fn["parameters"]["additionalProperties"] is False


def test_operation_spec_builds_one_tool_per_operation_and_validates():
    spec = operation_wrapper_spec(
        name="Weather API",
        base=EndpointConfig(url="https://api.example/v1"),
        description="wraps the weather API",
        operations=[
            OperationConfig(name="get_weather", method="GET", path="/weather",
                            params={"city": {"type": "string"}}, required=["city"]),
            OperationConfig(name="submit_report", method="POST", path="/reports",
                            params={"location": {"type": "string"}}, required=["location"]),
        ],
    )
    assert isinstance(spec, AgentSpec) and spec.mode == "llm"
    assert [s["function"]["name"] for s in spec.tool_schemas] == ["get_weather", "submit_report"]
    # Prompt lists each operation with its method + path so the model can route.
    assert "get_weather" in spec.system_prompt and "GET /weather" in spec.system_prompt
    assert "POST /reports" in spec.system_prompt
    spec.validate()  # alignment holds for every tool


def test_get_operation_sends_query_params():
    _HITS.clear()
    with _Server() as base_url:
        spec = operation_wrapper_spec(
            name="W", base=EndpointConfig(url=base_url),
            operations=[OperationConfig(
                name="get_weather", method="GET", path="/weather",
                params={"city": {"type": "string"}, "units": {"type": "string"}},
                required=["city"],
            )],
        )
        out = spec.dispatch("get_weather", {"city": "London", "units": "C"})
    assert out["ok"] and out["response"]["method"] == "GET"
    hit = _HITS[-1]
    assert hit["method"] == "GET"
    assert hit["path"].startswith("/weather?")
    assert "city=London" in hit["path"] and "units=C" in hit["path"]
    assert hit["body"] is None  # GET carries no body


def test_post_operation_sends_json_body():
    _HITS.clear()
    with _Server() as base_url:
        spec = operation_wrapper_spec(
            name="R", base=EndpointConfig(url=base_url),
            operations=[OperationConfig(
                name="submit_report", method="POST", path="/reports",
                params={"location": {"type": "string"}, "severity": {"type": "integer"}},
                required=["location"],
            )],
        )
        out = spec.dispatch("submit_report", {"location": "downtown", "severity": 3})
    assert out["ok"] and out["response"]["method"] == "POST"
    hit = _HITS[-1]
    assert hit["method"] == "POST" and hit["path"] == "/reports"
    assert hit["body"] == {"location": "downtown", "severity": 3}


def test_path_param_is_substituted_into_the_url():
    _HITS.clear()
    with _Server() as base_url:
        spec = operation_wrapper_spec(
            name="U", base=EndpointConfig(url=base_url),
            operations=[OperationConfig(
                name="get_user", method="GET", path="/users/{id}",
                params={"id": {"type": "integer"}}, required=["id"],
            )],
        )
        out = spec.dispatch("get_user", {"id": 42})
    assert out["ok"]
    assert _HITS[-1]["path"] == "/users/42"  # {id} filled, not sent as a query param


def test_missing_required_argument_returns_error_without_calling():
    _HITS.clear()
    with _Server() as base_url:
        spec = operation_wrapper_spec(
            name="W", base=EndpointConfig(url=base_url),
            operations=[OperationConfig(
                name="get_weather", method="GET", path="/weather",
                params={"city": {"type": "string"}}, required=["city"],
            )],
        )
        out = spec.dispatch("get_weather", {})
    assert out["ok"] is False
    assert "missing required argument 'city'" in out["error"]
    assert _HITS == []  # validation failed before any HTTP call


def test_wrong_argument_type_returns_error():
    spec = operation_wrapper_spec(
        name="W", base=EndpointConfig(url="https://api.example/v1"),
        operations=[OperationConfig(
            name="get_weather", method="GET", path="/weather",
            params={"city": {"type": "string"}}, required=["city"],
        )],
    )
    out = spec.dispatch("get_weather", {"city": 123})
    assert out["ok"] is False and "must be string" in out["error"]


def test_unexpected_argument_returns_error():
    spec = operation_wrapper_spec(
        name="W", base=EndpointConfig(url="https://api.example/v1"),
        operations=[OperationConfig(
            name="get_weather", method="GET", path="/weather",
            params={"city": {"type": "string"}}, required=["city"],
        )],
    )
    out = spec.dispatch("get_weather", {"city": "London", "bogus": 1})
    assert out["ok"] is False and "unexpected argument 'bogus'" in out["error"]


def test_empty_operations_raises():
    try:
        operation_wrapper_spec(name="Empty", base=EndpointConfig(url="https://a.example"), operations=[])
    except ValueError as exc:
        assert "at least one operation" in str(exc)
    else:
        raise AssertionError("expected ValueError for empty operations")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")
    print(f"ALL {len(fns)} PASSED")


if __name__ == "__main__":
    _run_all()
