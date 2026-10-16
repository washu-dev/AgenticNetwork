"""Mock LLM: the planner's three-step script and its directives, the specialist echo and tool
call, and the HTTP wire."""

from __future__ import annotations

import json
import threading
import urllib.error
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


def test_planner_step1_passes_a_skill_the_user_names():
    agents = [{**AGENTS[1], "skills": ["echo/summarize", "harbor/publish_notice"]}]
    listed = {"tool": "list_agents", "status": "ok", "content": {"agents": agents, "count": 1}}
    name, args = tool_call(mock_llm.build_completion(planner_body("Use harbor/publish_notice to echo it", [listed])))
    assert (name, args["skill"]) == ("call_agent", "harbor/publish_notice")
    # Without a named skill the call carries none, as before.
    _, args = tool_call(mock_llm.build_completion(planner_body("echo it", [listed])))
    assert "skill" not in args


def test_planner_ask_first_directive_asks_then_the_reply_runs_the_script():
    asked = tool_call(mock_llm.build_completion(planner_body(f"Echo my notes {mock_llm.ASK_FIRST}", [])))
    assert asked == ("ask_user", {"text": mock_llm.ASK_TEXT})
    # The reply is a new turn without the directive: the usual first step.
    assert tool_call(mock_llm.build_completion(planner_body("the harbor notes", []))) == ("list_agents", {})


def test_planner_never_answer_directive_lists_agents_at_every_step():
    text = f"Echo this {mock_llm.NEVER_ANSWER}"
    for observations in ([], [LIST_OK], [LIST_OK] * 7):
        assert tool_call(mock_llm.build_completion(planner_body(text, observations))) == ("list_agents", {})


CALCULATOR = {
    "type": "function",
    "function": {"name": "calculator", "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}}},
}
CLOCK = {"type": "function", "function": {"name": "current_time", "parameters": {"type": "object", "properties": {}}}}


def specialist_body(text: str, tools: list | None = None, results: list | None = None) -> dict:
    """A request shaped like agent_skeleton.llm_loop sends it, after ``results`` [(name, content)]."""
    messages = [
        {"role": "system", "content": "You are Harbor Tallyman."},
        {"role": "user", "content": json.dumps({"prompt": text}, indent=2)},
    ]
    for i, (name, content) in enumerate(results or []):
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}
        ]})
        messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": json.dumps(content)})
    body = {"model": "mock-specialist", "messages": messages}
    if tools:
        body["tools"] = tools
    return body


def test_specialist_calls_the_tool_its_message_names_with_the_json_after_it():
    text = 'Use the calculator {"expression": "17 * 23"} to total the crates.'
    assert tool_call(mock_llm.build_completion(specialist_body(text, [CLOCK, CALCULATOR]))) == (
        "calculator", {"expression": "17 * 23"},
    )
    # A named tool with no JSON after it is called without arguments.
    assert tool_call(mock_llm.build_completion(specialist_body("What is the current_time?", [CALCULATOR, CLOCK]))) == (
        "current_time", {},
    )


def test_specialist_without_a_named_offered_tool_echoes():
    for body in (
        specialist_body("Total the crates.", [CALCULATOR]),  # offered, not named
        specialist_body('Use the calculator {"expression": "1 + 1"}'),  # named, not offered
    ):
        choice = mock_llm.build_completion(body)["choices"][0]
        assert choice["finish_reason"] == "stop" and choice["message"]["content"].startswith("[mock-llm] Echo:")


def test_specialist_answers_with_the_tool_results_once_they_are_back():
    text = 'Use the calculator {"expression": "17 * 23"} to total the crates.'
    result = {"ok": True, "expression": "17 * 23", "result": 391}
    choice = mock_llm.build_completion(specialist_body(text, [CALCULATOR], [("calculator", result)]))["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == f"[mock-llm] Echo: {text} | calculator -> {json.dumps(result)}"


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


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_LLM_LOG", str(tmp_path / "requests.jsonl"))
    srv = mock_llm.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "requests.jsonl"
    srv.shutdown()
    srv.server_close()


def post(url: str, body: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


def test_http_models_completions_and_log(server):
    base, log = server
    with urllib.request.urlopen(f"{base}/v1/models", timeout=5) as resp:
        assert {m["id"] for m in json.loads(resp.read())["data"]} >= {"mock-planner", "mock-specialist"}

    status, raw = post(f"{base}/v1/chat/completions", planner_body("hi", []))
    assert status == 200 and json.loads(raw)["object"] == "chat.completion"

    status, raw = post(f"{base}/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "ping"}]})
    assert status == 200 and json.loads(raw)["choices"][0]["message"]["content"] == "[mock-llm] Echo: ping"

    with pytest.raises(urllib.error.HTTPError) as streamed:
        post(f"{base}/v1/chat/completions", {"model": "m", "stream": True, "messages": []})
    assert streamed.value.code == 400

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
