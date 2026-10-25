"""Goal 1 of docs/OPEN_SOURCE_RELEASE.md: a first-time user's whole session, end to end.

Black box: the suite drives the system only through `scripts/stack` and HTTP, so the same
tests can judge a component rebuilt from scratch. It needs Python >= 3.11 with pytest and
the two service venvs from docs/DEVSTACK.md; no other packages, network or keys.

    python -m pytest tests/pipeline -q -p no:cacheprovider

The tests share one stack and run in file order, like one session: start from nothing,
sign in, send a prompt, find it saved, watch it stream, register an agent and reach it,
check identity handling, shut down. test_scenarios.py reuses these helpers for the
scenarios beyond that session, on a stack of its own.

    PIPELINE_PORT_OFFSET  added to every stack port (default 20000), so the suite can run
                          next to a stack on the default ports
    PIPELINE_STATE_DIR    the stack's state dir (default <repo>/.devstack-pipeline). It is
                          reset (down, then the stack's files deleted) at the start; afterwards
                          the stack is stopped with this same dir and its logs/ are kept for
                          debugging.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "scripts" / "stack"
OFFSET = os.environ.get("PIPELINE_PORT_OFFSET", "20000")
STATE_DIR = Path(os.environ.get("PIPELINE_STATE_DIR") or REPO / ".devstack-pipeline").resolve()
# scripts/stack runs `python3`; use this interpreter (>= 3.11) unless DEVSTACK_PYTHON says otherwise.
STACK_ENV = {**os.environ, "DEVSTACK_PYTHON": os.environ.get("DEVSTACK_PYTHON") or sys.executable}
SEEDS = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((REPO / "devstack" / "seeds").glob("*.json"))]
SEED_AGENT = "Echo Assistant"
MARKER = "[mock-llm] Echo:"  # how every mock-LLM specialist reply starts
PROMPT = "Echo this back and summarize it: the full-chain pipeline test reached a specialist agent."
OTHER_USER = "other-user@example.test"  # a second signed-in identity

# A second llm agent, modelled on the Echo seed. The mock planner routes by shared words.
_ECHO = next(seed for seed in SEEDS if seed["name"] == SEED_AGENT)
ZEBRA = {
    **_ECHO,
    "name": "Zebra Scout",
    "description": "Answers questions about zebra migration across the savanna.",
    "config": {**_ECHO["config"], "system_prompt": "You are Zebra Scout. Answer questions about zebra migration."},
}
ZEBRA_PROMPT = "Where does the zebra migration go across the savanna?"


# ── The stack CLI ─────────────────────────────────────────────────────────────


def stack_cli(
    command: str, *args: str, timeout: float = 240, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run `scripts/stack COMMAND --state-dir STATE_DIR ARGS...` with ``env`` (default STACK_ENV)."""
    return subprocess.run(
        [str(STACK), command, "--state-dir", str(STATE_DIR), *args],
        cwd=REPO, env=env or STACK_ENV, capture_output=True, text=True, timeout=timeout,
    )


def stack_json(command: str, *args: str, expect: int = 0) -> dict[str, Any]:
    proc = stack_cli(command, *args, "--json")
    assert proc.returncode == expect, f"stack {command} exited {proc.returncode}:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def up(env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return stack_cli("up", "--json", "--llm", "mock", "--port-offset", OFFSET, env=env)


# ── HTTP (http.client never routes through an environment proxy) ──────────────


def _open(method: str, url: str, body: Any, headers: dict[str, str] | None, timeout: float):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    data = None if body is None else json.dumps(body).encode()
    sent = {"Content-Type": "application/json"} if data is not None else {}
    conn.request(method, parts.path, body=data, headers={**sent, **(headers or {})})
    return conn, conn.getresponse()


def call(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    """(status, JSON body or text)."""
    conn, resp = _open(method, url, body, headers, timeout=120)
    with contextlib.closing(conn):
        raw = resp.read()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, raw.decode("utf-8", "replace")


def stream(
    url: str, body: Any, headers: dict[str, str] | None = None, timeout: float = 120,
) -> tuple[int, str, list]:
    """POST and read a server-sent-event stream: (status, content type, [(event, data)])."""
    conn, resp = _open("POST", url, body, headers, timeout=timeout)
    events, name, data = [], "message", []
    with contextlib.closing(conn):
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and data:
                events.append((name, json.loads("\n".join(data))))
                name, data = "message", []
    return resp.status, resp.getheader("Content-Type") or "", events


def browser_body(text: str, context_id: str | None = None) -> dict[str, Any]:
    """The JSON the chat UI (chat_app/web/index.html) posts to /messages/stream."""
    return {"user_id": "demo-user", "channel": "api", "text": text, "context_id": context_id}


def chat(base: str, text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Send ``text`` the browser's way; return (final payload, the whole exchange)."""
    status, content_type, events = stream(f"{base}/messages/stream", browser_body(text))
    finals = [data for name, data in events if name == "final"]
    assert status == 200 and finals, f"/messages/stream -> {status}: {events}"
    return finals[-1], {"content_type": content_type, "events": events}


def listed_agents(base: str) -> dict[str, str]:
    """{name: agent_id} of the agents the planner can discover."""
    status, payload = call("GET", f"{base}/agents")
    assert status == 200, payload
    return {a["name"]: a["agent_id"] for a in payload["agents"]}


def tools(final: dict[str, Any]) -> list[str]:
    return [step.get("tool") for step in final["tool_trace"]]


def step(final: dict[str, Any], tool: str) -> dict[str, Any]:
    return next(s for s in final["tool_trace"] if s.get("tool") == tool)


def assert_delegated(final: dict[str, Any], agent_id: str, prompt: str) -> None:
    """Discovery (list_agents) -> delegation (call_agent) to ``agent_id`` -> final answer."""
    names = tools(final)
    assert {"list_agents", "call_agent", "final_answer"} <= set(names), names
    assert names.index("list_agents") < names.index("call_agent") < names.index("final_answer"), names
    assert final["state"] == "completed", final["text"]
    assert final["selected_agent"] == agent_id
    delegated = step(final, "call_agent")
    assert delegated["status"] == "completed" and delegated["content"]["agent_id"] == agent_id
    assert final["text"].startswith(MARKER) and prompt in final["text"], final["text"]
    assert step(final, "final_answer")["args"]["text"] == final["text"]


# ── Ports and processes ───────────────────────────────────────────────────────


def service_pids(status: dict[str, Any]) -> dict[str, int | None]:
    return {name: svc["pid"] for name, svc in status["services"].items()}


def stack_ports(status: dict[str, Any]) -> list[int]:
    first, last = status["agent_ports"]
    services = [urlsplit(s["url"]).port for s in status["services"].values() if s["url"].startswith("http")]
    return [*services, *range(first, last + 1)]


def listening(ports: list[int]) -> list[int]:
    busy = []
    for port in ports:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            # A port in the OS ephemeral range can "connect" to itself (TCP self-connect)
            # when nothing listens; that socket's own port is the target port.
            if sock.connect_ex(("127.0.0.1", port)) == 0 and sock.getsockname()[1] != port:
                busy.append(port)
    return busy


def live_processes() -> dict[int, int]:
    """{pid: parent pid} of every process that is not a zombie."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,stat="], capture_output=True, text=True, check=True).stdout
    rows = (line.split() for line in out.splitlines())
    return {int(row[0]): int(row[1]) for row in rows if len(row) >= 3 and not row[2].startswith("Z")}


def with_descendants(pids: set[int]) -> set[int]:
    table, found = live_processes(), set()
    todo = [p for p in pids if p in table]
    while todo:
        pid = todo.pop()
        if pid not in found:
            found.add(pid)
            todo.extend(child for child, parent in table.items() if parent == pid)
    return found


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _terminated(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt(f"signal {signum}")  # pytest still runs fixture teardown


@contextlib.contextmanager
def fresh_stack(env: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
    """The stack, started from nothing (with ``env`` for `up`, default STACK_ENV). Always
    stopped with the same state dir, even when `up` or a test fails or the run is
    terminated; the dir is only cleared by the next run's `reset`, which stops whatever it
    still records first."""
    if STATE_DIR.exists():
        reset = stack_cli("reset")
        assert reset.returncode == 0, f"stack reset exited {reset.returncode}:\n{reset.stderr}"
    previous = signal.signal(signal.SIGTERM, _terminated)
    try:
        started = up(env)
        assert started.returncode == 0, f"stack up exited {started.returncode}:\n{started.stdout}\n{started.stderr}"
        status = json.loads(started.stdout)
        not_ready = {name: svc["detail"] for name, svc in status["services"].items() if not svc["ready"]}
        assert status["ready"] and not not_ready, not_ready
        yield {
            "up": status,
            "proxy": status["services"]["proxy"]["url"],
            "chat_app": status["services"]["chat-app"]["url"],
            "user": status["dev_user"],
        }
    finally:
        down = stack_cli("down")
        signal.signal(signal.SIGTERM, previous)
    assert down.returncode == 0, f"stack down exited {down.returncode}:\n{down.stderr}"
    assert not listening(stack_ports(stack_json("status", expect=1))), "stack ports still listening after down"


@pytest.fixture(scope="module")
def stack() -> Any:
    """This file's stack; see fresh_stack."""
    with fresh_stack() as started:
        yield started


@pytest.fixture(scope="module")
def first_turn(stack: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The user's first prompt, sent through the front door exactly as the chat UI sends it."""
    return chat(stack["proxy"], PROMPT)


# ── The session, in order ─────────────────────────────────────────────────────


def test_1_up_from_nothing_is_ready_and_idempotent(stack):
    started = stack["up"]
    # Fresh registry and directory: exactly the seed agents, nothing left from earlier runs.
    assert sorted(started["agents"]) == sorted(seed["name"] for seed in SEEDS)
    status = stack_json("status")
    assert status["ready"] and all(svc["ready"] for svc in status["services"].values()), status
    # Running up again changes nothing: same processes, no new starts or registrations.
    again = up()
    assert again.returncode == 0, again.stderr
    assert service_pids(json.loads(again.stdout)) == service_pids(started)
    assert "starting" not in again.stderr and "registering" not in again.stderr, again.stderr


def test_2_first_signed_in_request_creates_the_user(stack):
    status, me = call("GET", f"{stack['proxy']}/me")
    assert status == 200 and me["email"] == stack["user"] and me["id"], me
    # The front door decides who you are: identity headers sent by the browser are dropped.
    forged = {"mail": "intruder@example.test", "eppn": "intruder@example.test", "remote-user": "intruder"}
    assert call("GET", f"{stack['proxy']}/me", headers=forged) == (200, me)
    # The planner maps the identity header the proxy sends to that same account ...
    assert call("GET", f"{stack['chat_app']}/me", headers={"mail": stack["user"]}) == (200, me)
    # ... and a new identity gets a new, non-admin account on its first request.
    status, other = call("GET", f"{stack['chat_app']}/me", headers={"mail": OTHER_USER})
    assert status == 200 and other["email"] == OTHER_USER and other["id"] != me["id"] and not other["is_admin"]


def test_3_prompt_is_answered_by_the_seed_agent(stack, first_turn):
    final, _ = first_turn
    assert_delegated(final, listed_agents(stack["proxy"])[SEED_AGENT], PROMPT)
    discovered = step(final, "list_agents")["content"]["agents"]
    assert SEED_AGENT in [agent["name"] for agent in discovered]


def test_4_conversation_is_saved_for_that_user_only(stack, first_turn):
    final, _ = first_turn
    cid = final["context_id"]
    status, sessions = call("GET", f"{stack['proxy']}/sessions")
    assert status == 200 and cid in [s["context_id"] for s in sessions["sessions"]], sessions
    status, history = call("GET", f"{stack['proxy']}/conversations/{cid}")
    assert status == 200, history
    messages = [(m["role"], m["text"]) for m in history["messages"]]
    assert messages[0] == ("user", PROMPT) and messages[-1] == ("assistant", final["text"]), messages
    # The same account, reached directly with the header the proxy sends, sees it too.
    assert call("GET", f"{stack['chat_app']}/conversations/{cid}", headers={"mail": stack["user"]})[0] == 200
    # Another signed-in user can neither list, read, write to nor delete it.
    other = {"mail": OTHER_USER}
    status, theirs = call("GET", f"{stack['chat_app']}/sessions", headers=other)
    assert status == 200 and cid not in [s["context_id"] for s in theirs["sessions"]]
    assert call("GET", f"{stack['chat_app']}/conversations/{cid}", headers=other)[0] == 403
    assert call("POST", f"{stack['chat_app']}/messages", browser_body("hi", cid), headers=other)[0] == 403
    assert call("DELETE", f"{stack['chat_app']}/conversations/{cid}", headers=other)[0] == 403
    assert call("GET", f"{stack['proxy']}/conversations/{cid}") == (200, history)


def test_5_stream_sends_progress_then_one_final_event(first_turn):
    final, exchange = first_turn
    assert exchange["content_type"].startswith("text/event-stream")
    names = [name for name, _ in exchange["events"]]
    assert names[-1] == "final" and names.count("final") == 1 and "error" not in names, names
    progress = [data.get("tool") for name, data in exchange["events"] if name == "trace"]
    # Every step reached the browser as it happened, before the final answer.
    assert progress == tools(final) and {"list_agents", "call_agent"} <= set(progress), progress


def test_6_registered_agent_receives_the_prompt_that_matches_it(stack, tmp_path):
    definition = tmp_path / "zebra_scout.json"
    definition.write_text(json.dumps(ZEBRA, indent=2), encoding="utf-8")
    added = stack_json("add-agent", str(definition))
    assert added["listed_by_planner"] and added["ads_cid"], added
    assert listed_agents(stack["proxy"]).get(ZEBRA["name"]) == added["chat_app_agent_id"]
    assert ZEBRA["name"] in stack_json("status")["agents"]

    final, _ = chat(stack["proxy"], ZEBRA_PROMPT)
    assert_delegated(final, added["chat_app_agent_id"], ZEBRA_PROMPT)
    # The reply came from the new agent's own LLM call, made with its registered prompt.
    log = stack_cli("logs", "mock-llm-requests", "-n", "20").stdout.splitlines()
    answered = [
        entry["response"]["choices"][0]["message"]["content"]
        for entry in map(json.loads, log)
        if entry["kind"] == "specialist"
        and any(m["role"] == "system" and m["content"] == ZEBRA["config"]["system_prompt"]
                for m in entry["request"]["messages"])
    ]
    assert final["text"] in answered, answered


def test_7_body_user_id_is_ignored_when_a_login_identity_is_present(stack, first_turn):
    victim = "victim@example.test"
    # Through the front door: the conversation belongs to the signed-in user, not the body's.
    status, reply = call("POST", f"{stack['proxy']}/messages", {"user_id": victim, "text": PROMPT, "channel": "api"})
    assert status == 200 and reply["state"] == "completed", reply
    cid = reply["context_id"]
    assert call("GET", f"{stack['proxy']}/conversations/{cid}")[0] == 200
    assert call("GET", f"{stack['chat_app']}/conversations/{cid}", headers={"mail": victim})[0] == 403
    # Naming the owner in the body does not let another user write into their conversation.
    owner_cid = first_turn[0]["context_id"]
    spoof = {"user_id": stack["user"], "channel": "api", "text": "hi", "context_id": owner_cid}
    assert call("POST", f"{stack['chat_app']}/messages", spoof, headers={"mail": OTHER_USER})[0] == 403
    status, _, events = stream(f"{stack['chat_app']}/messages/stream", spoof, headers={"mail": OTHER_USER})
    assert status == 200 and [name for name, _ in events] == ["error"], events


def test_8_down_leaves_nothing_running(stack):
    before = stack_json("status")
    ports = stack_ports(before)
    services = {pid for pid in service_pids(before).values() if pid}
    processes = with_descendants(services)
    assert len(processes) >= len(services) + len(before["agents"]), processes  # services and agents
    down = stack_cli("down")
    assert down.returncode == 0, down.stderr
    assert not listening(ports), "ports still listening after down"
    deadline = time.monotonic() + 10  # exited processes may wait a moment to be reaped
    while processes & set(live_processes()) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not processes & set(live_processes()), "stack processes still running after down"
    after = stack_json("status", expect=1)
    assert not after["ready"] and not any(svc["running"] for svc in after["services"].values()), after
