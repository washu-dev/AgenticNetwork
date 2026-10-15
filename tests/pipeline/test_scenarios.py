"""Black-box scenarios beyond the first-time session in test_full_chain.py.

Same rules as that file: the tests drive the system only through `scripts/stack`, HTTP and
the dev stack's fake `dirctl`, offline with the mock LLM, and share one stack in file order.
It is a stack of its own (same PIPELINE_PORT_OFFSET and PIPELINE_STATE_DIR, started after
test_full_chain.py's has stopped) whose planner gives up on a silent agent after
A2A_TIMEOUT seconds instead of its default 30.

    python -m pytest tests/pipeline/test_scenarios.py -q -p no:cacheprovider

Agents that the tests run themselves are published straight into the stack's directory
with the fake `dirctl` (`push`, then `routing publish`), as an operator publishes a card;
the planner discovers them like any other agent. Each test unpublishes them again.
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
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest

from test_full_chain import (
    REPO, SEEDS, SEED_AGENT, STACK_ENV, ZEBRA, ZEBRA_PROMPT, assert_delegated, browser_body,
    call, chat, fresh_stack, listed_agents, listening, service_pids, stack_cli, stack_json,
    step, stream, up,
)

A2A_TIMEOUT = 5  # seconds: the planner's A2A_HTTP_TIMEOUT_SECONDS on this file's stack
SCENARIO_ENV = {
    **STACK_ENV,
    "DEVSTACK_PASSTHROUGH_ENV": ",".join(
        filter(None, [STACK_ENV.get("DEVSTACK_PASSTHROUGH_ENV"), "A2A_HTTP_TIMEOUT_SECONDS"])
    ),
    "A2A_HTTP_TIMEOUT_SECONDS": str(A2A_TIMEOUT),
}
DIRCTL = REPO / "devstack" / "bin" / "dirctl"
# How a card declares the user credentials it needs (registration_service/card_credentials.py).
CREDENTIALS_EXTENSION = "agenticnetwork.dev/ext/required-credentials"
BUNDLE = {  # one user's stored credentials (placeholder values)
    "openai_api_key": {"api_key": "test-openai-value"},
    "brave_api_key": {"api_key": "test-brave-value"},
}
RECORDER_MARKER = "[recorder] received at"  # how the recording agent's replies start


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
    ) -> None:
        """Publish a card for an agent at ``url``: the seed agent's record with its card swapped."""
        routed = (self.dirctl("pull", ref["record_ref"]["cid"]) for ref in self.dirctl("routing", "list"))
        record = next(record for record in routed if card_of(record)["name"] == SEED_AGENT)
        card = card_of(record)
        card.update(name=name, description=description, url=url, capabilities={"streaming": False})
        card["skills"] = [{**card["skills"][0], "description": description}]
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
    message naming the path it arrived at."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.server.received.append((self.path, request))
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


@pytest.fixture(scope="module")
def recorder() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingAgent)
    server.received = []
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

    This tier has no credential store (the planner's /me/credentials routes need Postgres),
    so the request carries the bundle as metadata.credentials, where the planner's server
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


def test_3_registered_agent_is_discoverable_again_after_a_restart(stack, tmp_path):
    definition = tmp_path / "zebra_scout.json"
    definition.write_text(json.dumps(ZEBRA, indent=2), encoding="utf-8")
    added = stack_json("add-agent", str(definition))
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
    assert listed_agents(stack["proxy"])[ZEBRA["name"]] == added["planner_agent_id"]
    final, _ = chat(stack["proxy"], ZEBRA_PROMPT)
    assert_delegated(final, added["planner_agent_id"], ZEBRA_PROMPT)
