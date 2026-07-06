"""Tests for the multi-endpoint "manager" spec (Feature B core).

Stdlib-only; the only network is a localhost stub (mirrors test_wrapper). Run:
    python -m pytest agent_skeleton/tests/test_manager.py -q
    python -m agent_skeleton.tests.test_manager
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent_skeleton.spec import AgentSpec, multi_endpoint_wrapper_spec
from agent_skeleton.system_tools.call_endpoint import EndpointConfig, endpoint_tool_name

_HITS: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length)
        _HITS.append(self.path)  # record which endpoint was actually called
        resp = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


class _Server:
    def __enter__(self):
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def __exit__(self, *a):
        self.srv.shutdown()


def test_endpoint_tool_name_slugs_and_falls_back():
    assert endpoint_tool_name("Weather API", 0) == "call_weather_api"
    assert endpoint_tool_name("", 3) == "call_endpoint_3"
    assert endpoint_tool_name("  Fancy/Name!! ", 1) == "call_fancy_name"
    # Result is always a valid Chat Completions tool name.
    import re

    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", endpoint_tool_name("x" * 200, 0))


def test_manager_builds_one_tool_per_endpoint_and_validates():
    spec = multi_endpoint_wrapper_spec(
        name="Ops Manager",
        description="routes ops requests",
        endpoints=[
            EndpointConfig(url="https://a.example/x", name="Weather", description="forecasts"),
            EndpointConfig(url="https://b.example/y", name="Tickets", description="support tickets"),
        ],
    )
    assert isinstance(spec, AgentSpec) and spec.mode == "llm"
    names = [s["function"]["name"] for s in spec.tool_schemas]
    assert names == ["call_weather", "call_tickets"]
    # Each tool's description carries its endpoint's routing info.
    weather_schema = spec.tool_schemas[0]["function"]
    assert "Weather" in weather_schema["description"] and "forecasts" in weather_schema["description"]
    # Prompt lists both endpoints so the model can route.
    assert "call_weather" in spec.system_prompt and "call_tickets" in spec.system_prompt
    assert "support tickets" in spec.system_prompt
    spec.validate()  # alignment holds for every tool


def test_manager_routes_each_tool_to_its_own_endpoint():
    # Two distinct stub servers; confirm each tool hits ONLY its own URL.
    _HITS.clear()
    with _Server() as base_a, _Server() as base_b:
        spec = multi_endpoint_wrapper_spec(
            name="Router",
            endpoints=[
                EndpointConfig(url=f"{base_a}/alpha", name="Alpha"),
                EndpointConfig(url=f"{base_b}/beta", name="Beta"),
            ],
        )
        out_a = spec.dispatch("call_alpha", {"request": "hi alpha"})
        out_b = spec.dispatch("call_beta", {"request": "hi beta"})
    assert out_a["ok"] and out_a["response"]["path"] == "/alpha"
    assert out_b["ok"] and out_b["response"]["path"] == "/beta"
    assert set(_HITS) == {"/alpha", "/beta"}


def test_manager_dedupes_duplicate_and_empty_labels():
    spec = multi_endpoint_wrapper_spec(
        name="Dupes",
        endpoints=[
            EndpointConfig(url="https://a.example/1", name="Search"),
            EndpointConfig(url="https://a.example/2", name="Search"),  # same label
            EndpointConfig(url="https://a.example/3"),                  # empty label
        ],
    )
    names = [s["function"]["name"] for s in spec.tool_schemas]
    assert len(names) == len(set(names)) == 3  # all unique
    assert names[0] == "call_search" and names[1].startswith("call_search")
    spec.validate()


def test_manager_empty_endpoints_raises():
    try:
        multi_endpoint_wrapper_spec(name="Empty", endpoints=[])
    except ValueError as exc:
        assert "at least one endpoint" in str(exc)
    else:
        raise AssertionError("expected ValueError for empty endpoints")


def test_manager_single_endpoint_is_valid():
    spec = multi_endpoint_wrapper_spec(
        name="Solo", endpoints=[EndpointConfig(url="https://a.example/x", name="Only")]
    )
    assert [s["function"]["name"] for s in spec.tool_schemas] == ["call_only"]
    spec.validate()


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")
    print(f"ALL {len(fns)} PASSED")


if __name__ == "__main__":
    _run_all()
