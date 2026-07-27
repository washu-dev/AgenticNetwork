"""Tests for the spec-driven engine + call_endpoint system tool.

Deliberately dependency-free: stdlib only (no a2a-sdk, no openai, no network beyond
a localhost stub). Run from the AgenticNetwork repo root:

    python -m pytest agent_skeleton/tests -q
    # or, without pytest:
    python -m agent_skeleton.tests.test_wrapper
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent_skeleton import llm_loop
from agent_skeleton.spec import (
    AgentSpec,
    default_demo_spec,
    endpoint_wrapper_spec,
)
from agent_skeleton.system_tools.call_endpoint import EndpointConfig, make_call_endpoint

_CAPTURED: dict = {}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence test server
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode()) if length else {}
        _CAPTURED["body"] = body
        _CAPTURED["auth"] = self.headers.get("Authorization")
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/a2a":
            resp = {
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"status": {"message": {"parts": [{"kind": "text", "text": "A2A says hi"}]}}},
            }
        else:
            resp = {"echo": body, "ok": True}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Server:
    def __enter__(self):
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def __exit__(self, *a):
        self.srv.shutdown()


def test_http_call_endpoint_posts_and_parses():
    with _Server() as base:
        _, fn = make_call_endpoint(EndpointConfig(url=f"{base}/x"))
        r = fn(request="hello", payload={"k": 1})
    assert r["ok"] and r["status_code"] == 200
    assert _CAPTURED["body"] == {"input": "hello", "k": 1}
    assert r["response"]["echo"]["input"] == "hello"


def test_a2a_protocol_wraps_envelope_and_extracts_text():
    with _Server() as base:
        _, fn = make_call_endpoint(EndpointConfig(url=f"{base}/a2a", protocol="a2a"))
        r = fn(request="ping")
    assert _CAPTURED["body"]["method"] == "message/send"
    assert _CAPTURED["body"]["params"]["message"]["parts"][0]["text"] == "ping"
    assert r["text"] == "A2A says hi"


def test_auth_token_read_from_env():
    os.environ["MY_TOK"] = "s3cret"
    with _Server() as base:
        _, fn = make_call_endpoint(EndpointConfig(url=f"{base}/x", auth_env="MY_TOK"))
        fn(request="z")
        assert _CAPTURED["auth"] == "Bearer s3cret"
        _, fn_raw = make_call_endpoint(EndpointConfig(url=f"{base}/x", auth_env="MY_TOK", auth_scheme="raw"))
        fn_raw(request="z")
        assert _CAPTURED["auth"] == "s3cret"


def test_connection_error_is_caught():
    _, fn = make_call_endpoint(EndpointConfig(url="http://127.0.0.1:1/nope", timeout=1))
    r = fn(request="z")
    assert r["ok"] is False and "error" in r


def test_a2a_ids_are_unique_per_call():
    with _Server() as base:
        _, fn = make_call_endpoint(EndpointConfig(url=f"{base}/a2a", protocol="a2a"))
        fn(request="one")
        id1 = _CAPTURED["body"]["params"]["message"]["messageId"]
        fn(request="two")
        id2 = _CAPTURED["body"]["params"]["message"]["messageId"]
    assert id1 != id2 and id1 != "wrapper-task"


def test_redirects_not_followed():
    with _Server() as base:
        _, fn = make_call_endpoint(EndpointConfig(url=f"{base}/redirect"))
        r = fn(request="z")
    assert r["ok"] is False and r["status_code"] == 302


def test_response_size_capped():
    with _Server() as base:
        _, fn = make_call_endpoint(EndpointConfig(url=f"{base}/x", max_response_bytes=10))
        r = fn(request="z")
    assert r["ok"] and r.get("truncated") is True
    assert isinstance(r["response"], str) and len(r["response"]) == 10


def test_non_http_scheme_rejected():
    for bad in ("file:///etc/passwd", "ftp://host/x", "gopher://x"):
        try:
            EndpointConfig(url=bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")


def test_error_body_redacts_secrets():
    # the redactor masks secret-shaped substrings in an upstream error body
    from agent_skeleton.system_tools.call_endpoint import _redact
    masked = _redact("oops Authorization: Bearer sk-12345 and api_key=abcdef")
    assert "sk-12345" not in masked and "abcdef" not in masked and "<redacted>" in masked
    # Basic auth credentials are masked too (multi-endpoint managers may use them).
    basic = _redact("401 Authorization: Basic dXNlcjpwYXNzd29yZA==")
    assert "dXNlcjpwYXNzd29yZA==" not in basic and "<redacted>" in basic


def test_wrapper_spec_validates_and_dispatches():
    with _Server() as base:
        spec = endpoint_wrapper_spec(
            name="Weather",
            endpoint=EndpointConfig(url=f"{base}/x"),
            description="weather API",
            io_criteria="city in, forecast out",
        )
        assert isinstance(spec, AgentSpec) and spec.mode == "llm"
        assert [s["function"]["name"] for s in spec.tool_schemas] == ["call_endpoint"]
        out = spec.dispatch("call_endpoint", {"request": "weather in SF"})
        assert out["ok"]
    assert "call_endpoint" in spec.system_prompt and "Weather" in spec.system_prompt
    assert "answer" in spec.system_prompt and "tools_used" in spec.system_prompt
    assert spec.dispatch("nope", {})["ok"] is False  # unknown tool is graceful


def test_default_demo_spec_backward_compatible():
    demo = default_demo_spec()
    demo.validate()
    assert demo.dispatch("word_count", {"text": "a b c"})["word_count"] == 3


def test_run_agent_threads_spec_through_frozen_loop():
    seen: dict = {}

    def fake_loop(*, model, system_prompt, user_payload, tool_schemas, dispatch, max_steps):
        seen.update(model=model, names=[s["function"]["name"] for s in tool_schemas], max_steps=max_steps)
        dispatch("call_endpoint", {"request": "x"})  # exercise dispatch wiring
        return '{"answer": "done", "tools_used": ["call_endpoint"]}', [{"name": "call_endpoint"}]

    with _Server() as base:
        spec = endpoint_wrapper_spec(name="W", endpoint=EndpointConfig(url=f"{base}/x"))
        orig = llm_loop.run_tool_loop
        llm_loop.run_tool_loop = fake_loop
        try:
            result = llm_loop.run_agent({"prompt": "hi"}, spec=spec, model="m1")
        finally:
            llm_loop.run_tool_loop = orig
    assert seen["model"] == "m1" and seen["names"] == ["call_endpoint"]
    assert result["answer"] == "done" and result["tools_used"] == ["call_endpoint"]
    assert result["response_text"] == "done"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")
    print(f"ALL {len(fns)} PASSED")


if __name__ == "__main__":
    _run_all()
