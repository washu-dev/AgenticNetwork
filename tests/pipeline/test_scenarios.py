"""Black-box scenarios beyond the first-time session in test_full_chain.py.

Same rules as that file: the tests drive the system only through `scripts/stack`, HTTP and
the dev stack's fake `dirctl`, offline with the mock LLM, and share one stack in file order.
It is a stack of its own (same PIPELINE_PORT_OFFSET and PIPELINE_STATE_DIR, started after
test_full_chain.py's has stopped) whose planner gives up on a silent agent after
A2A_TIMEOUT seconds instead of its default 30, stops a turn after MAX_STEPS tool steps
instead of 10, and runs the `strict` policy mode, in which a call that names a write skill
waits for the user's confirmation. The mock planner passes a skill only when the prompt
names one, so the other scenarios are unaffected.

    scripts/stack test scenarios   # ports PIPELINE_PORT_OFFSET + 100, state dir .devstack-scenarios
    python -m pytest tests/pipeline/test_scenarios.py -q -p no:cacheprovider

Agents that the tests run themselves are published straight into the stack's directory
with the fake `dirctl` (`push`, then `routing publish`), as an operator publishes a card;
the planner discovers them like any other agent. Each test unpublishes them again. Agents a
test registers with `add-agent` (the `register` fixture) stay live until `down`, but their
saved definitions are deleted afterwards, so the restart test's `up` registers only its own.

Two directives in a prompt script the mock planner (devstack/mock_llm.py): ASK_FIRST makes it
ask the user before anything else, NEVER_ANSWER makes it list agents at every step.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from test_full_chain import (
    OTHER_USER, REPO, SEEDS, SEED_AGENT, STACK_ENV, ZEBRA, ZEBRA_PROMPT, assert_delegated,
    browser_body, call, chat, fresh_stack, listed_agents, listening, service_pids, stack_cli,
    stack_json, step, stream, tools, up,
)

A2A_TIMEOUT = 5  # seconds: the planner's A2A_HTTP_TIMEOUT_SECONDS on this file's stack
MAX_STEPS = 6  # the planner's PLANNER_MAX_STEPS on this file's stack
PLANNER_SETTINGS = {
    "A2A_HTTP_TIMEOUT_SECONDS": str(A2A_TIMEOUT),
    "PLANNER_MAX_STEPS": str(MAX_STEPS),
    "PLANNER_POLICY_MODE": "strict",
}
SCENARIO_ENV = {
    **STACK_ENV,
    "DEVSTACK_PASSTHROUGH_ENV": ",".join(
        filter(None, [STACK_ENV.get("DEVSTACK_PASSTHROUGH_ENV"), *PLANNER_SETTINGS])
    ),
    **PLANNER_SETTINGS,
}
DIRCTL = REPO / "devstack" / "bin" / "dirctl"
# How a card declares the user credentials it needs (registration_service/card_credentials.py).
CREDENTIALS_EXTENSION = "agenticnetwork.dev/ext/required-credentials"
BUNDLE = {  # one user's stored credentials (placeholder values)
    "openai_api_key": {"api_key": "test-openai-value"},
    "brave_api_key": {"api_key": "test-brave-value"},
}
RECORDER_MARKER = "[recorder] received at"  # how the recording agent's replies start
# Mock-LLM directives and the question it asks (devstack/mock_llm.py).
ASK_FIRST = "[mock-llm: ask first]"
NEVER_ANSWER = "[mock-llm: never answer]"
ASK_TEXT = "Before I hand this to an agent: which details matter most to you?"


def add_agent(definition: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    """Register ``definition`` with `scripts/stack add-agent`; return its summary."""
    path = tmp_path / f"{definition['name'].lower().replace(' ', '_')}.json"
    path.write_text(json.dumps(definition, indent=2), encoding="utf-8")
    added = stack_json("add-agent", str(path))
    assert added["listed_by_planner"] and added["ads_cid"], added
    return added


def llm_exchanges(kind: str, first_user_text: str) -> list[dict[str, Any]]:
    """The mock LLM's logged exchanges of ``kind`` ("planner" or "specialist") whose first
    user message is, or (for a specialist) carries as its prompt, ``first_user_text``."""
    log = stack_cli("logs", "mock-llm-requests", "-n", "500").stdout.splitlines()
    found = []
    for entry in map(json.loads, log):
        users = [m.get("content") for m in entry["request"]["messages"] if m.get("role") == "user"]
        try:
            payload = json.loads(users[0])
        except (IndexError, TypeError, ValueError):
            continue
        if (entry["kind"] == kind and isinstance(payload, dict)
                and first_user_text in (payload.get("user_text"), payload.get("prompt"))):
            found.append(entry)
    return found


# ── The directory, through the fake dirctl ───────────────────────────────────


def card_of(record: dict[str, Any]) -> dict[str, Any]:
    """The A2A card a directory record carries (its `integration/a2a` module)."""
    return next(m["data"]["card_data"] for m in record["modules"] if "card_data" in (m.get("data") or {}))


class Directory:
    """The stack's agent directory, read and written with the dev stack's fake `dirctl`."""

    def __init__(self, state_file: str) -> None:
        self.env = {**STACK_ENV, "FAKE_DIRCTL_STATE": state_file}
        self.published: list[str] = []

    def dirctl(self, *args: str, record: dict[str, Any] | None = None) -> Any:
        proc = subprocess.run(
            [sys.executable, str(DIRCTL), *args, "--output", "json"], env=self.env, timeout=30,
            input=None if record is None else json.dumps(record), capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"dirctl {' '.join(args)} exited {proc.returncode}: {proc.stderr}"
        return json.loads(proc.stdout) if proc.stdout.lstrip().startswith(("{", "[")) else proc.stdout

    def publish(
        self, name: str, description: str, url: str, credentials: list[dict[str, Any]] | None = None,
        skill: str | None = None,
    ) -> None:
        """Publish a card for an agent at ``url``: the seed agent's record with its card swapped,
        and its one skill's id replaced by ``skill`` if given."""
        routed = (self.dirctl("pull", ref["record_ref"]["cid"]) for ref in self.dirctl("routing", "list"))
        record = next(record for record in routed if card_of(record)["name"] == SEED_AGENT)
        card = card_of(record)
        card.update(name=name, description=description, url=url, capabilities={"streaming": False})
        card["skills"] = [{**card["skills"][0], "description": description, **({"id": skill} if skill else {})}]
        if credentials:
            card["capabilities"]["extensions"] = [
                {"uri": CREDENTIALS_EXTENSION, "required": False, "params": {"credentials": credentials}}
            ]
        agent_id = name.lower().replace(" ", "-")
        record.update(name=f"https://example.invalid/agents/{agent_id}", description=description)
        record["annotations"].update({
            "a2a.url": url, "agentic_network.agent_id": agent_id, "agentic_network.display_name": name,
            "agentic_network.record_id": str(uuid.uuid4()),
        })
        record["locators"] = [{"type": "source_code", "urls": [url], "annotations": {"a2a.url": url}}]
        cid = self.dirctl("push", "-", record=record)["cid"]
        self.dirctl("routing", "publish", cid)
        self.published.append(cid)

    def unpublish_all(self) -> None:
        while self.published:
            self.dirctl("routing", "unpublish", self.published.pop())


def wait_listed(base: str, *names: str, timeout: float = 15) -> dict[str, str]:
    """{name: agent_id} once the planner lists every one of ``names``."""
    deadline = time.monotonic() + timeout
    while not set(names) <= set(agents := listed_agents(base)):
        assert time.monotonic() < deadline, f"planner never listed {names}: {sorted(agents)}"
        time.sleep(0.2)
    return agents


# ── A recording A2A agent ─────────────────────────────────────────────────────


class _RecordingAgent(BaseHTTPRequestHandler):
    """A minimal A2A agent: records each JSON-RPC request and answers it with one text
    message naming the path it arrived at, after the server's ``delay`` seconds."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.server.received.append((self.path, request))
        time.sleep(self.server.delay)
        reply = {"jsonrpc": "2.0", "id": request.get("id"), "result": {
            "kind": "message", "role": "agent", "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": f"{RECORDER_MARKER} {self.path}"}],
        }}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def stack() -> Any:
    """This file's stack (see test_full_chain.fresh_stack), with the short A2A timeout."""
    with fresh_stack(SCENARIO_ENV) as started:
        yield started


@pytest.fixture
def directory(stack: dict[str, Any]) -> Iterator[Directory]:
    found = Directory(urlsplit(stack["up"]["services"]["directory"]["url"]).path)
    try:
        yield found
    finally:
        found.unpublish_all()


@pytest.fixture
def register(stack: dict[str, Any], tmp_path: Path) -> Iterator[Callable[[dict[str, Any]], dict[str, Any]]]:
    """add-agent for one test. Afterwards the saved definition is deleted (as `add-agent`
    tells a user to drop an agent), so a later `up` does not register the agent again."""
    saved: list[Path] = []

    def register(definition: dict[str, Any]) -> dict[str, Any]:
        added = add_agent(definition, tmp_path)
        saved.append(Path(added["saved_to"]))
        return added

    try:
        yield register
    finally:
        for path in saved:
            path.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def recorder() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingAgent)
    server.received = []
    server.delay = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


# ── Scenarios ─────────────────────────────────────────────────────────────────


def test_1_agents_receive_only_the_credentials_they_declare(stack, directory, recorder):
    """From one user's credential bundle, an agent that declares a type gets just that type
    and an agent that declares none gets none.

    This tier has no credential store (the chat app's /me/credentials routes need Postgres),
    so the request carries the bundle as metadata.credentials, where the chat app's server
    puts the stored credentials it loads. Grants are not exercised: without a database the
    planner passes the declared types only.
    """
    base = f"http://127.0.0.1:{recorder.server_address[1]}"
    directory.publish(
        "Ledger Keeper", "Reconciles ledger entries for the quarterly audit.", f"{base}/declares-openai/",
        credentials=[{"type": "openai_api_key", "label": "OpenAI API key", "required": True}],
    )
    directory.publish("Weather Almanac", "Forecasts rainfall for the harvest season.", f"{base}/declares-none/")
    agents = wait_listed(stack["proxy"], "Ledger Keeper", "Weather Almanac")
    cases = [
        ("Ledger Keeper", "Which ledger entries need reconciling for the quarterly audit?", "/declares-openai/",
         {"openai_api_key": BUNDLE["openai_api_key"]}),
        ("Weather Almanac", "What rainfall is expected for the harvest season?", "/declares-none/", None),
    ]
    for name, prompt, path, expected in cases:
        recorder.received.clear()
        body = {**browser_body(prompt), "metadata": {"credentials": BUNDLE}}
        status, _, events = stream(f"{stack['proxy']}/messages/stream", body)
        assert status == 200 and events and events[-1][0] == "final", events
        final = events[-1][1]
        assert final["selected_agent"] == agents[name] and step(final, "call_agent")["status"] == "completed", final
        assert final["text"] == f"{RECORDER_MARKER} {path}", final["text"]  # the answer came from that agent
        [(received_at, request)] = recorder.received
        assert received_at == path
        assert request["params"]["metadata"].get("credentials") == expected, request["params"]["metadata"]


UNREACHABLE = {  # failure: (name, description, prompt)
    "refused": ("Lighthouse Keeper", "Reports lamp status for lighthouses along the rocky coast.",
                "What is the lamp status of the lighthouses along the rocky coast?"),
    "silent": ("Glacier Surveyor", "Measures glacier drift across the frozen fjord.",
               "How far did the glacier drift across the frozen fjord?"),
}


@pytest.mark.parametrize("failure", sorted(UNREACHABLE))
def test_2_unreachable_agent_ends_the_turn_with_a_clear_error(stack, directory, failure):
    """An agent that refuses the connection, or accepts it and never answers, fails the call
    with an error naming the agent; the turn ends normally, within a bounded time."""
    name, description, prompt = UNREACHABLE[failure]
    with contextlib.ExitStack() as cleanup:
        if failure == "refused":
            port = stack["up"]["agent_ports"][1]  # the last agent port of this stack: nothing listens there
            assert not listening([port])
        else:
            silent = cleanup.enter_context(socket.socket())
            silent.bind(("127.0.0.1", 0))
            silent.listen()  # the kernel accepts the connection; nobody reads or replies
            port = silent.getsockname()[1]
        directory.publish(name, description, f"http://127.0.0.1:{port}/")
        agent_id = wait_listed(stack["proxy"], name)[name]
        started = time.monotonic()
        status, _, events = stream(f"{stack['proxy']}/messages/stream", browser_body(prompt), timeout=A2A_TIMEOUT + 60)
        elapsed = time.monotonic() - started

    names = [event for event, _ in events]
    assert status == 200 and names and names[-1] == "final", names
    assert names.count("final") == 1 and "error" not in names, names
    final = events[-1][1]
    call_step = step(final, "call_agent")
    assert final["selected_agent"] == agent_id and call_step["status"] == "failed", call_step
    assert call_step["content"]["structured_output"]["status"] == "a2a_call_failed", call_step
    assert name in call_step["content"]["text"], call_step  # the error names the agent
    assert final["state"] == "completed" and agent_id in final["text"], final["text"]  # and reaches the user
    # A refused connection fails at once; a silent agent is cut off by the A2A timeout.
    assert elapsed < (A2A_TIMEOUT if failure == "refused" else A2A_TIMEOUT + 10), elapsed


# A `proxy` agent has no LLM: it relays each task to its upstream A2A endpoint.
RELAY = {
    "name": "Courier Relay",
    "description": "Forwards telegrams from the mountain observatory to the duty officer.",
    "handler_type": "proxy",
    "oasf_skills": next(seed for seed in SEEDS if seed["name"] == SEED_AGENT)["oasf_skills"],
}
RELAY_PROMPT = "Forward this telegram from the mountain observatory: a storm front arrives at dusk."


def test_3_proxy_agent_relays_the_task_to_its_upstream(stack, register):
    """A `proxy` agent registered through the registration path (add-agent: register, spawn,
    publish) relays the planner's task to its upstream, here the seed agent's A2A endpoint,
    and the seed agent's answer comes back through it."""
    status, listing = call("GET", f"{stack['proxy']}/agents")
    assert status == 200, listing
    upstream = next(a["endpoint"] for a in listing["agents"] if a["name"] == SEED_AGENT)
    added = register({**RELAY, "config": {"upstream_url": upstream}})

    final, _ = chat(stack["proxy"], RELAY_PROMPT)
    assert_delegated(final, added["chat_app_agent_id"], RELAY_PROMPT)
    relayed = step(final, "call_agent")["content"]["structured_output"]
    assert relayed["agent_id"] == added["id"] and relayed["upstream_url"] == upstream.rstrip("/"), relayed
    # The answer was written by the seed agent's own LLM call, made with its registered prompt.
    seed_prompt = next(seed for seed in SEEDS if seed["name"] == SEED_AGENT)["config"]["system_prompt"]
    [exchange] = llm_exchanges("specialist", RELAY_PROMPT)
    assert exchange["request"]["messages"][0] == {"role": "system", "content": seed_prompt}
    assert exchange["response"]["choices"][0]["message"]["content"] == final["text"]


# An `llm` agent armed with a tool from the preset catalog (agent_skeleton/system_tools).
TALLYMAN = {
    "name": "Cargo Tallyman",
    "description": "Totals crate counts for the cargo dock with a calculator.",
    "handler_type": "llm",
    "oasf_skills": RELAY["oasf_skills"],
    "config": {
        "system_prompt": "You are Cargo Tallyman. Total crate counts with the calculator.",
        "model": "mock-specialist",
        "tools": {"source": "presets", "names": ["calculator"]},
    },
}
TALLY_PROMPT = 'Use the calculator {"expression": "17 * 23"} to total the crates at the cargo dock.'


def test_4_llm_agent_uses_its_preset_tool(stack, register):
    """An `llm` agent registered with the preset `calculator` tool offers it to its model,
    runs the call the model makes inside the agent, and answers with the result."""
    added = register(TALLYMAN)
    final, _ = chat(stack["proxy"], TALLY_PROMPT)
    assert_delegated(final, added["chat_app_agent_id"], TALLY_PROMPT)
    # 17 * 23 was computed by the agent's calculator; the mock LLM only relays the result.
    result = {"ok": True, "expression": "17 * 23", "result": 391}
    assert final["text"].endswith(f" | calculator -> {json.dumps(result)}"), final["text"]
    assert step(final, "call_agent")["content"]["structured_output"]["tools_used"] == ["calculator"]
    # Two model calls: the first offered the catalog's calculator and got a call back, the
    # second carried the tool's result.
    first, second = llm_exchanges("specialist", TALLY_PROMPT)
    assert first["request"]["messages"][0]["content"].startswith(TALLYMAN["config"]["system_prompt"])
    assert [t["function"]["name"] for t in first["request"]["tools"]] == ["calculator"]
    [called] = first["response"]["choices"][0]["message"]["tool_calls"]
    assert json.loads(called["function"]["arguments"]) == {"expression": "17 * 23"}
    tool_message = second["request"]["messages"][-1]
    assert tool_message["role"] == "tool" and json.loads(tool_message["content"]) == result


def test_5_step_limit_ends_a_turn_that_never_answers(stack):
    """A planner model that never calls final_answer is cut off after MAX_STEPS steps: the
    turn ends with one final event that says so, not a hang or a stream error."""
    prompt = f"Keep searching the directory for a lighthouse navigator. {NEVER_ANSWER}"
    started = time.monotonic()
    status, _, events = stream(f"{stack['proxy']}/messages/stream", browser_body(prompt), timeout=60)
    elapsed = time.monotonic() - started

    names = [event for event, _ in events]
    assert status == 200 and names and names[-1] == "final", names
    assert names.count("final") == 1 and "error" not in names, names
    final = events[-1][1]
    assert tools(final) == ["list_agents"] * MAX_STEPS, tools(final)
    assert final["state"] == "input_required" and final["errors"] == ["max_steps_exceeded"], final
    assert "maximum number of tool steps" in final["text"] and final["selected_agent"] is None, final
    # The planner stopped asking its model after MAX_STEPS calls.
    assert len(llm_exchanges("planner", prompt)) == MAX_STEPS
    assert elapsed < 30, elapsed


def test_6_ask_user_pauses_the_turn_and_the_reply_continues_it(stack):
    """The planner asks the user a question (ask_user); the turn ends waiting for input. The
    reply, sent into the same conversation, reaches an agent, and the planner's model sees the
    question and the first request when it picks one."""
    question = f"Summarize my notes for me. {ASK_FIRST}"
    asked, _ = chat(stack["proxy"], question)
    assert asked["state"] == "input_required" and asked["text"] == ASK_TEXT, asked
    assert tools(asked) == ["ask_user"] and asked["selected_agent"] is None, asked
    assert not asked["confirmation_required"], asked
    cid = asked["context_id"]

    reply = "Echo this back and summarize it: the pier notes reached a specialist agent."
    status, _, events = stream(f"{stack['proxy']}/messages/stream", browser_body(reply, cid))
    assert status == 200 and events and events[-1][0] == "final", events
    final = events[-1][1]
    assert final["context_id"] == cid
    assert_delegated(final, listed_agents(stack["proxy"])[SEED_AGENT], reply)
    first_step = llm_exchanges("planner", reply)[0]["request"]["messages"][1]["content"]
    assert json.loads(first_step)["conversation_history"] == [
        f"user: {question}", f"assistant: {ASK_TEXT}", f"user: {reply}",
    ]
    status, history = call("GET", f"{stack['proxy']}/conversations/{cid}")
    assert status == 200, history
    assert [(m["role"], m["text"]) for m in history["messages"]] == [
        ("user", question), ("assistant", ASK_TEXT), ("user", reply), ("assistant", final["text"]),
    ]


# An agent with a write skill (its id holds "publish"): strict policy holds such calls.
NOTICES = ("Harbor Notices", "Posts tide table notices on the harbor board.")
NOTICE_SKILL = "harbor/publish_notice"
NOTICE_PROMPT = f"Use {NOTICE_SKILL} to post tomorrow's tide table on the harbor board."


def ask_to_publish(stack: dict[str, Any], directory: Directory, recorder: ThreadingHTTPServer) -> dict[str, Any]:
    """Publish the notices agent and ask for its write skill; return the turn awaiting confirmation."""
    if not directory.published:
        url = f"http://127.0.0.1:{recorder.server_address[1]}/notices/"
        directory.publish(*NOTICES, url, skill=NOTICE_SKILL)
    agent_id = wait_listed(stack["proxy"], NOTICES[0])[NOTICES[0]]
    recorder.received.clear()
    asked, _ = chat(stack["proxy"], NOTICE_PROMPT)
    assert asked["state"] == "input_required" and asked["confirmation_required"], asked
    pending = asked["pending_confirmation"]
    assert (pending["agent_id"], pending["skill"]) == (agent_id, NOTICE_SKILL), pending
    assert step(asked, "call_agent")["status"] == "confirmation_required" and NOTICE_SKILL in asked["text"]
    assert recorder.received == []  # nothing ran yet
    return asked


def test_7_write_skill_runs_only_after_its_owner_confirms(stack, directory, recorder):
    """In strict mode a call to a write skill waits for confirmation. Only the conversation's
    owner can confirm or cancel it, and only for the prompt the confirm button was shown on;
    the confirmed call reaches the agent once, marked as confirmed; a "no" cancels it without
    reaching the agent."""
    asked = ask_to_publish(stack, directory, recorder)
    cid = asked["context_id"]
    for confirmed in (True, False):
        body = {"context_id": cid, "confirmed": confirmed}
        assert call("POST", f"{stack['chat_app']}/confirmations", body, headers={"mail": OTHER_USER})[0] == 403
    assert recorder.received == []

    # The owner confirms, the way the chat UI's confirm button does: naming the prompt it
    # showed. A press naming another prompt (one a newer prompt replaced) runs nothing.
    confirm = {"user_id": "demo-user", "channel": "api", "context_id": cid, "confirmed": True}
    status, stale = call("POST", f"{stack['proxy']}/confirmations", {**confirm, "prompt_id": "0" * 32})
    assert status == 200 and stale["errors"] == ["superseded"] and recorder.received == [], stale
    confirm["prompt_id"] = asked["pending_confirmation"]["prompt_id"]
    status, done = call("POST", f"{stack['proxy']}/confirmations", confirm)
    assert status == 200 and done["state"] == "completed", done
    assert done["text"] == f"{RECORDER_MARKER} /notices/" and done["selected_agent"], done
    [(_, request)] = recorder.received
    metadata = request["params"]["metadata"]
    assert metadata["confirmed"] is True and metadata["confirmation_status"] == "confirmed", metadata
    assert metadata["skill"] == NOTICE_SKILL, metadata
    # Nothing is pending any more: a second confirm runs nothing.
    status, again = call("POST", f"{stack['proxy']}/confirmations", confirm)
    assert status == 200 and again["state"] == "failed" and len(recorder.received) == 1, again

    cid = ask_to_publish(stack, directory, recorder)["context_id"]
    status, _, events = stream(f"{stack['proxy']}/messages/stream", browser_body("no", cid))
    assert status == 200 and events and events[-1][0] == "final", events
    assert events[-1][1]["text"] == f"Cancelled. {NOTICE_SKILL} was not executed.", events[-1]
    assert recorder.received == []


def test_8_another_users_yes_or_no_does_not_touch_a_pending_action(stack, directory, recorder):
    """A chat reply of "yes" or "no" from another signed-in user, naming the owner's
    conversation, is refused like any other message into it, and the action stays pending."""
    cid = ask_to_publish(stack, directory, recorder)["context_id"]
    for reply in ("yes", "no"):
        body = {"user_id": OTHER_USER, "channel": "api", "text": reply, "context_id": cid}
        status, answer = call("POST", f"{stack['chat_app']}/messages", body, headers={"mail": OTHER_USER})
        assert status == 403 and recorder.received == [], (reply, status, answer)
    confirm = {"context_id": cid, "confirmed": True}
    status, done = call("POST", f"{stack['proxy']}/confirmations", confirm)
    assert status == 200 and done["state"] == "completed" and len(recorder.received) == 1, done


def test_9_two_confirmations_at_once_run_the_write_once(stack, directory, recorder):
    """A double click on the confirm button sends two confirmations together. The agent
    answers slowly, so the second arrives while the first is still inside its call; only
    one of them runs the write. The dev stack has one planner process, so the other is
    refused with answer_in_progress (no_pending if it came after the first had finished)."""
    cid = ask_to_publish(stack, directory, recorder)["context_id"]
    confirm = {"user_id": "demo-user", "channel": "api", "context_id": cid, "confirmed": True}
    recorder.delay = 1
    try:
        with ThreadPoolExecutor(2) as pool:
            answers = list(pool.map(lambda _: call("POST", f"{stack['proxy']}/confirmations", confirm), range(2)))
    finally:
        recorder.delay = 0
    assert [status for status, _ in answers] == [200, 200], answers
    assert sorted(answer["state"] for _, answer in answers) == ["completed", "failed"], answers
    [refused] = [answer for _, answer in answers if answer["state"] == "failed"]
    assert refused["errors"] in (["answer_in_progress"], ["no_pending"]), refused
    assert len(recorder.received) == 1, recorder.received


def test_10_registered_agent_is_discoverable_again_after_a_restart(stack, tmp_path):
    added = add_agent(ZEBRA, tmp_path)
    before = stack_json("status")

    down = stack_cli("down")
    assert down.returncode == 0, down.stderr
    started = up(SCENARIO_ENV)
    assert started.returncode == 0, f"stack up exited {started.returncode}:\n{started.stdout}\n{started.stderr}"
    after = json.loads(started.stdout)
    assert after["ready"] and ZEBRA["name"] in after["agents"], after
    # The same state dir, served by new processes.
    assert not {pid for pid in service_pids(before).values() if pid} & set(service_pids(after).values())

    # Listed once each: no stale records from before the restart.
    status, listing = call("GET", f"{stack['proxy']}/agents")
    assert status == 200, listing
    assert sorted(a["name"] for a in listing["agents"]) == sorted([*(s["name"] for s in SEEDS), ZEBRA["name"]])
    assert listed_agents(stack["proxy"])[ZEBRA["name"]] == added["chat_app_agent_id"]
    final, _ = chat(stack["proxy"], ZEBRA_PROMPT)
    assert_delegated(final, added["chat_app_agent_id"], ZEBRA_PROMPT)
