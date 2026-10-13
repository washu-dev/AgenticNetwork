"""Mock LLM: the planner's three-step script, the specialist echo, and the HTTP wire."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from devstack import mock_llm

PLANNER_TOOLS = [
    {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}
    for name in ("list_agents", "describe_agent", "call_agent", "ask_user", "final_answer")
]
AGENTS = [
    {"agent_id": "weather-agent", "name": "Weather Agent", "description": "Forecasts and current weather."},
    {"agent_id": "echo-assistant", "name": "Echo Assistant", "description": "Echoes and summarizes text."},
]


def planner_body(user_text: str, observations: list[dict]) -> dict:
    """A request shaped like planner_agent/main_agent/llm_client.py sends it."""
    messages = [
        {"role": "system", "content": "You are the planner."},
        {"role": "user", "content": json.dumps({"user_text": user_text, "context_id": "ctx-1", "instructions": "..."})},
    ]
    for i, obs in enumerate(observations):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"call_{i}", "type": "function", "function": {"name": obs["tool"], "arguments": "{}"}}],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": json.dumps(obs)})
    return {"model": "mock-planner", "messages": messages, "tools": PLANNER_TOOLS, "tool_choice": "required"}


def tool_call(completion: dict) -> tuple[str, dict]:
    choice = completion["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function" and call["id"]
    return call["function"]["name"], json.loads(call["function"]["arguments"])


LIST_OK = {"tool": "list_agents", "status": "ok", "content": {"agents": AGENTS, "count": 2}}


def test_planner_step0_lists_agents():
    assert tool_call(mock_llm.build_completion(planner_body("hi", []))) == ("list_agents", {})


def test_planner_step1_calls_best_matching_agent():
    name, args = tool_call(mock_llm.build_completion(planner_body("Please echo this text", [LIST_OK])))
    assert name == "call_agent"
    assert args == {"agent_id": "echo-assistant", "task_input": {"request": "Please echo this text"}}


def test_planner_step1_falls_back_to_first_agent():
    name, args = tool_call(mock_llm.build_completion(planner_body("zzz qqq", [LIST_OK])))
    assert (name, args["agent_id"]) == ("call_agent", "weather-agent")


def test_planner_step1_without_agents_answers():
    empty = {"tool": "list_agents", "status": "ok", "content": {"agents": [], "note": "ADS returned zero agents."}}
    assert tool_call(mock_llm.build_completion(planner_body("hi", [empty]))) == (
        "final_answer",
        {"text": mock_llm.NO_AGENTS_TEXT},
    )


def test_planner_step1_reports_listing_error():
    failed = {"tool": "list_agents", "status": "error", "content": {"error": "directory down"}}
    name, args = tool_call(mock_llm.build_completion(planner_body("hi", [failed])))
    assert name == "final_answer" and "directory down" in args["text"]


def test_planner_step2_returns_agent_text():
    called = {
        "tool": "call_agent",
        "status": "completed",
        "content": {"agent_id": "echo-assistant", "state": "completed", "text": "[mock-llm] Echo: hi"},
    }
    assert tool_call(mock_llm.build_completion(planner_body("hi", [LIST_OK, called]))) == (
        "final_answer",
        {"text": "[mock-llm] Echo: hi"},
    )


def test_planner_step2_explains_failed_call():
    failed = {
        "tool": "call_agent",
        "status": "failed",
        "content": {
            "agent_id": "echo-assistant",
            "text": "A2A call to Echo Assistant failed: ConnectError",
            "structured_output": {"status": "a2a_call_failed", "error": "ConnectError"},
        },
    }
    name, args = tool_call(mock_llm.build_completion(planner_body("hi", [LIST_OK, failed])))
    assert name == "final_answer"
    assert "did not succeed" in args["text"] and "ConnectError" in args["text"]


def test_specialist_echo_unwraps_skeleton_payload():
    # agent_skeleton.llm_loop sends json.dumps({"prompt": user_text}, indent=2).
    body = {
        "model": "mock-specialist",
        "messages": [
            {"role": "system", "content": "You are Echo Assistant."},
            {"role": "user", "content": json.dumps({"prompt": "summarize   this\nplease"}, indent=2)},
        ],
    }
    completion = mock_llm.build_completion(body)
    choice = completion["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"] == {"role": "assistant", "content": "[mock-llm] Echo: summarize this please"}


def test_specialist_echo_is_deterministic_and_short():
    body = {"model": "m", "messages": [{"role": "user", "content": "x" * 1000}]}
    first = mock_llm.build_completion(body)
    second = mock_llm.build_completion(body)
    assert first["choices"] == second["choices"]
    assert len(first["choices"][0]["message"]["content"]) < 400


def test_stream_chunks_carry_tool_call_and_finish():
    chunks = mock_llm.stream_chunks(mock_llm.build_completion(planner_body("hi", [])))
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "list_agents"
    assert chunks[2]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_LLM_LOG", str(tmp_path / "requests.jsonl"))
    srv = mock_llm.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "requests.jsonl"
    srv.shutdown()
    srv.server_close()


def post(url: str, body: dict) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read(), resp.headers.get("Content-Type", "")


def test_http_models_completions_stream_and_log(server):
    base, log = server
    with urllib.request.urlopen(f"{base}/v1/models", timeout=5) as resp:
        assert {m["id"] for m in json.loads(resp.read())["data"]} >= {"mock-planner", "mock-specialist"}

    status, raw, _ = post(f"{base}/v1/chat/completions", planner_body("hi", []))
    assert status == 200 and json.loads(raw)["object"] == "chat.completion"

    status, raw, ctype = post(
        f"{base}/v1/chat/completions",
        {"model": "m", "stream": True, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert ctype.startswith("text/event-stream")
    events = [line[6:] for line in raw.decode().splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    text = "".join(
        c["choices"][0]["delta"].get("content", "") for c in map(json.loads, events[:-1]) if c["choices"]
    )
    assert text == "[mock-llm] Echo: ping"

    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["kind"] for e in entries] == ["planner", "specialist"]
    assert entries[0]["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "list_agents"


def test_openai_sdk_parses_replies(server):
    openai = pytest.importorskip("openai")
    base, _ = server
    client = openai.OpenAI(base_url=f"{base}/v1", api_key="mock-key")
    planner = planner_body("hi", [])
    response = client.chat.completions.create(
        model="mock-planner", messages=planner["messages"], tools=planner["tools"], tool_choice="required"
    )
    assert response.choices[0].message.tool_calls[0].function.name == "list_agents"

    specialist = client.chat.completions.create(model="mock-specialist", messages=[{"role": "user", "content": "yo"}])
    assert specialist.choices[0].message.content == "[mock-llm] Echo: yo"

    stream = client.chat.completions.create(
        model="mock-specialist", messages=[{"role": "user", "content": "yo"}], stream=True
    )
    assert "".join(c.choices[0].delta.content or "" for c in stream if c.choices) == "[mock-llm] Echo: yo"
