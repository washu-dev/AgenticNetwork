"""End-to-end coverage for the autonomous intelligence-agent executor.

These tests inject:
- a stub completion_fn into ``IntelligenceAgentExecutor`` to drive the LLM
  loop deterministically (no real model);
- a stub directory client whose ``search_agents`` returns canned matches;
- a monkeypatched ``_send_a2a_request`` on the module so the peer A2A call
  is intercepted (no real peer agent ASGI app spun up);
- an ``httpx.MockTransport`` for Brave Search so ``web_search`` returns
  predictable hits.

The whole agent loop runs end-to-end (RequestContext -> EventQueue ->
A2ATaskResult), so all integration glue between the tools and the trace
synthesis is exercised.

Run from the AgenticNetwork repo root with the v1 venv::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \\
    PYTHONPATH=intelligence_agent \\
    v1_classification/.venv/bin/python -m pytest \\
        intelligence_agent/tests -q
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest


_THIS = Path(__file__).resolve()
_AGENT_DIR = _THIS.parents[1]
_REPO_ROOT = _AGENT_DIR.parents[1]


def _load_intel_agent_module():
    spec = importlib.util.spec_from_file_location(
        "external_intelligence_agent", _AGENT_DIR / "intelligence_agent.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("external_intelligence_agent", module)
    spec.loader.exec_module(module)
    return module


intel_agent = _load_intel_agent_module()
sys.path.insert(0, str(_AGENT_DIR))

from _vendor_v2.a2a.adapters import a2a_agent_card_to_discovered_agent
from _vendor_v2.a2a.client import A2AClient
from _vendor_v2.a2a.transport import HTTPA2ATransport
from _vendor_v2.common.models import (
    A2AMessage,
    A2APart,
    A2ATaskRequest,
    A2ATaskState,
)


INTEL_CARD = _AGENT_DIR / "intelligence_agent.card.json"


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


# --- LLM completion stub ----------------------------------------------------


def _stub_response(*, content=None, tool_calls=None):
    raw_calls = []
    for i, (name, args) in enumerate(tool_calls or []):
        raw_calls.append(
            types.SimpleNamespace(
                id=f"call_{i}",
                type="function",
                function=types.SimpleNamespace(
                    name=name,
                    arguments=json.dumps(args),
                ),
            )
        )
    msg = types.SimpleNamespace(
        content=content,
        tool_calls=raw_calls or None,
    )
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def _completion_stub(plan):
    """Async ``acompletion`` stub that yields ``plan`` steps in order."""
    iterator = iter(plan)

    async def acompletion(**kwargs):
        try:
            step = next(iterator)
        except StopIteration:
            raise AssertionError("agent loop continued past planned steps")
        kind = step[0]
        if kind == "final":
            return _stub_response(content=step[1])
        if kind == "tool":
            return _stub_response(tool_calls=[(step[1], step[2])])
        raise AssertionError(f"unknown plan step {step!r}")

    return acompletion


# --- Fake directory + peer A2A stub ----------------------------------------


class _FakeAgent:
    def __init__(self, *, agent_id, name, endpoint, description="", skills=()):
        self.agent_id = agent_id
        self.name = name
        self.endpoint = endpoint
        self.description = description
        self.transport = "http"
        self.source_record_id = f"cid-{agent_id}"
        self.source_card_url = endpoint
        self.skills = list(skills)


class _FakeDirectory:
    def __init__(self, by_skill: dict[str, list[_FakeAgent]] | None = None,
                 raise_on_search: Exception | None = None):
        self.by_skill = by_skill or {}
        self.raise_on_search = raise_on_search
        self.calls: list[str] = []

    def search_agents(self, *, skill: str):
        self.calls.append(skill)
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return list(self.by_skill.get(skill, []))


def _patched_send_factory(canned_by_agent_id):
    """Build a replacement for ``intel_agent._send_a2a_request``.

    ``canned_by_agent_id`` maps agent_id -> dict-result. The dict shape
    matches what the real ``_send_a2a_request`` returns:
    ``{state, summary, structured_output, message_id, error}``.
    """
    sent: list[dict] = []

    async def fake_send(*, agent, skill, prompt, context_id, transport):
        sent.append({
            "agent_id": agent.agent_id,
            "skill": skill,
            "prompt": prompt,
            "context_id": context_id,
        })
        canned = canned_by_agent_id.get(agent.agent_id) or {
            "state": "completed",
            "summary": f"(no canned response for {agent.agent_id})",
            "structured_output": None,
            "message_id": None,
            "error": None,
        }
        return dict(canned)

    fake_send.sent = sent  # type: ignore[attr-defined]
    return fake_send


# --- Brave Search HTTP mock -------------------------------------------------


def _brave_mock_transport(canned_results):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "params": dict(request.url.params),
        })
        body = {
            "web": {
                "results": [
                    {
                        "title": r["title"],
                        "url": r["url"],
                        "description": r.get("snippet", ""),
                        "age": r.get("age"),
                    }
                    for r in canned_results
                ]
            }
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


# --- End-to-end driver ------------------------------------------------------


async def _send(prompt, *, plan, directory, monkeypatch=None,
                canned_peer=None, brave_results=None, brave_api_key=None):
    """Drive one A2A request end-to-end and return the result dict."""
    card = intel_agent.load_intel_a2a_agent_card(INTEL_CARD)

    canned_peer = canned_peer or {}
    fake_send = _patched_send_factory(canned_peer)
    if monkeypatch is not None:
        monkeypatch.setattr(intel_agent, "_send_a2a_request", fake_send)

    brave_client = None
    if brave_results is not None:
        brave_transport = _brave_mock_transport(brave_results)
        brave_client = httpx.AsyncClient(transport=brave_transport)

    executor = intel_agent.IntelligenceAgentExecutor(
        directory=directory,
        completion_fn=_completion_stub(plan),
        brave_api_key=brave_api_key,
        brave_http_client=brave_client,
    )
    app = intel_agent.create_intel_a2a_app(card, executor=executor)
    discovered = a2a_agent_card_to_discovered_agent(card)

    request = A2ATaskRequest(
        task_id="t-1",
        context_id="ctx-1",
        target_agent_id=discovered.agent_id,
        skill="intelligence/situation_summary",
        user_goal=prompt,
        messages=[A2AMessage(role="user", parts=[A2APart(kind="text", text=prompt)])],
        metadata={"skill": "intelligence/situation_summary"},
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=card.url,
        ) as http_client:
            transport = HTTPA2ATransport(
                http_clients_by_origin={_origin(card.url): http_client}
            )
            client = A2AClient(transport)
            return (await client.send_task(discovered, request)).model_dump(mode="json"), \
                   getattr(fake_send, "sent", []), \
                   brave_client
    finally:
        if brave_client is not None:
            await brave_client.aclose()


# --- Tests ------------------------------------------------------------------


def test_full_loop_calls_recon_peer_then_web_search_then_synthesizes(monkeypatch) -> None:
    recon = _FakeAgent(
        agent_id="recon-mock",
        name="Recon Mock",
        endpoint="http://127.0.0.1:9201/",
        description="Spider-robot recon mock",
        skills=["recon/spider_robots"],
    )
    directory = _FakeDirectory(by_skill={"recon/spider_robots": [recon]})

    canned = {
        "recon-mock": {
            "state": "completed",
            "summary": (
                "Spider robots ALPHA-1..3 hold positions in grid ALPHA-3. "
                "Visual on 1x T-72 hull-down 230m east of OBJ-RED. "
                "2x civilians (non-combatant) observed ~150m north."
            ),
            "structured_output": {"observed_entities": ["T-72", "civilian", "civilian"]},
            "message_id": "m-1",
            "error": None,
        }
    }

    plan = [
        ("tool", "query_peer_agent", {
            "skill_query": "recon/spider_robots",
            "prompt": "Current contacts and civilian presence in grid ALPHA-3?",
        }),
        ("tool", "web_search", {
            "query": "T-72 vulnerability hull-down ATGM doctrine",
            "count": 3,
        }),
        ("final",
         "SITUATIONAL AWARENESS: 1x T-72 hull-down 230m E of OBJ-RED; "
         "2x civilians 150m N. THREAT ASSESSMENT: hostile armor (high "
         "confidence); civilians (high confidence). COURSE OF ACTION: "
         "Recommend ATGM standoff from south flank, hold fires NW arc "
         "to protect civilians. INFORMATION GAPS: civilian intent "
         "unknown; no thermal."),
    ]

    brave_results = [
        {"title": "T-72 hull-down vulnerabilities", "url": "https://ex/1",
         "snippet": "Top-attack ATGMs effective ...", "age": "2d"},
        {"title": "ATGM standoff doctrine", "url": "https://ex/2",
         "snippet": "Engagement ranges ..."},
        {"title": "Civilian harm mitigation", "url": "https://ex/3",
         "snippet": "Rules of engagement ..."},
    ]

    result, peer_sent, _ = asyncio.run(_send(
        "Brief grid ALPHA-3.",
        plan=plan,
        directory=directory,
        monkeypatch=monkeypatch,
        canned_peer=canned,
        brave_results=brave_results,
        brave_api_key="brave-test-key",
    ))

    assert result["state"] == A2ATaskState.completed.value
    structured = result["structured_output"]

    # Peer call recorded with the right shape.
    assert len(structured["peer_calls"]) == 1
    pc = structured["peer_calls"][0]
    assert pc["status"] == "ok"
    assert pc["agent_id"] == "recon-mock"
    assert pc["skill_query"] == "recon/spider_robots"
    assert "T-72" in pc["reply_text"]
    assert pc["structured_output"]["observed_entities"].count("civilian") == 2

    # _send_a2a_request actually got called with the right peer + prompt.
    assert len(peer_sent) == 1
    assert peer_sent[0]["agent_id"] == "recon-mock"
    assert "ALPHA-3" in peer_sent[0]["prompt"]
    # Tool propagated the request's context_id (ctx-1 from _send).
    assert peer_sent[0]["context_id"] == "ctx-1"

    # Web search recorded.
    assert len(structured["web_search_calls"]) == 1
    ws = structured["web_search_calls"][0]
    assert ws["status"] == "ok"
    assert ws["count"] == 3
    assert {r["url"] for r in ws["results"]} == {"https://ex/1", "https://ex/2", "https://ex/3"}

    # Tool-call trace lists the LLM's three tool invocations in order.
    tool_names = [tc["name"] for tc in structured["agent"]["tool_calls"]]
    assert tool_names == ["query_peer_agent", "web_search"]

    # Final brief lands in agent.final_message and the message text.
    assert "SITUATIONAL AWARENESS" in structured["agent"]["final_message"]
    assert "COURSE OF ACTION" in structured["agent"]["final_message"]
    assert structured["agent"]["exit_reason"] == "completed"


def test_query_peer_with_no_match_returns_no_agent_found(monkeypatch) -> None:
    directory = _FakeDirectory(by_skill={})  # nothing matches

    plan = [
        ("tool", "query_peer_agent", {
            "skill_query": "recon/spider_robots",
            "prompt": "status?",
        }),
        ("final",
         "INFORMATION GAPS: recon channel unavailable — no spider-robot "
         "agent advertised on the network. Cannot produce situational "
         "awareness without forward sensor coverage."),
    ]

    result, peer_sent, _ = asyncio.run(_send(
        "Brief.",
        plan=plan,
        directory=directory,
        monkeypatch=monkeypatch,
    ))

    assert result["state"] == A2ATaskState.completed.value
    structured = result["structured_output"]
    assert len(structured["peer_calls"]) == 1
    assert structured["peer_calls"][0]["status"] == "no_agent_found"
    # No A2A request was actually sent because discovery returned empty.
    assert peer_sent == []
    # The LLM's final message acknowledges the gap.
    assert "GAPS" in structured["agent"]["final_message"]


def test_web_search_without_brave_key_returns_not_configured(monkeypatch) -> None:
    directory = _FakeDirectory()

    plan = [
        ("tool", "web_search", {"query": "anything"}),
        ("final", "Web search unavailable; relying on peers only."),
    ]

    result, _, _ = asyncio.run(_send(
        "Brief.",
        plan=plan,
        directory=directory,
        monkeypatch=monkeypatch,
        # brave_api_key omitted -> falls through to env.
        brave_api_key=None,
    ))
    structured = result["structured_output"]
    assert len(structured["web_search_calls"]) == 1
    assert structured["web_search_calls"][0]["status"] == "not_configured"


def test_web_search_calls_brave_with_api_key_header(monkeypatch) -> None:
    directory = _FakeDirectory()

    plan = [
        ("tool", "web_search", {"query": "T-72 doctrine", "count": 2}),
        ("final", "ok"),
    ]

    canned = [
        {"title": "doc1", "url": "https://x/1", "snippet": "..."},
        {"title": "doc2", "url": "https://x/2", "snippet": "..."},
    ]

    # Build the brave client manually so we can inspect the captured request.
    brave_transport = _brave_mock_transport(canned)
    brave_client = httpx.AsyncClient(transport=brave_transport)

    card = intel_agent.load_intel_a2a_agent_card(INTEL_CARD)
    executor = intel_agent.IntelligenceAgentExecutor(
        directory=directory,
        completion_fn=_completion_stub(plan),
        brave_api_key="brave-test-key",
        brave_http_client=brave_client,
    )
    app = intel_agent.create_intel_a2a_app(card, executor=executor)
    discovered = a2a_agent_card_to_discovered_agent(card)
    request = A2ATaskRequest(
        task_id="t-1",
        context_id="ctx-1",
        target_agent_id=discovered.agent_id,
        skill="intelligence/situation_summary",
        user_goal="brief",
        messages=[A2AMessage(role="user", parts=[A2APart(kind="text", text="brief")])],
        metadata={"skill": "intelligence/situation_summary"},
    )

    async def _go():
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=card.url,
            ) as http_client:
                transport = HTTPA2ATransport(
                    http_clients_by_origin={_origin(card.url): http_client}
                )
                client = A2AClient(transport)
                return await client.send_task(discovered, request)
        finally:
            await brave_client.aclose()

    result = asyncio.run(_go()).model_dump(mode="json")
    structured = result["structured_output"]
    assert structured["web_search_calls"][0]["status"] == "ok"
    assert structured["web_search_calls"][0]["count"] == 2

    # The Brave HTTP call carried our API key header and the right query/count.
    assert len(brave_transport.seen) == 1
    seen = brave_transport.seen[0]
    assert seen["headers"]["x-subscription-token"] == "brave-test-key"
    assert seen["params"]["q"] == "T-72 doctrine"
    assert int(seen["params"]["count"]) == 2


def test_directory_unavailable_surfaces_in_tool_result(monkeypatch) -> None:
    directory = _FakeDirectory(raise_on_search=RuntimeError("ADS down"))

    plan = [
        ("tool", "query_peer_agent", {
            "skill_query": "recon/spider_robots",
            "prompt": "status?",
        }),
        ("final",
         "INFORMATION GAPS: directory is unreachable; cannot enumerate "
         "available recon peers."),
    ]

    result, peer_sent, _ = asyncio.run(_send(
        "Brief.",
        plan=plan,
        directory=directory,
        monkeypatch=monkeypatch,
    ))

    structured = result["structured_output"]
    assert len(structured["peer_calls"]) == 1
    assert structured["peer_calls"][0]["status"] == "directory_unavailable"
    assert "ADS down" in structured["peer_calls"][0]["error"]
    assert peer_sent == []


def test_repeated_query_peer_calls_share_discovery_cache(monkeypatch) -> None:
    recon = _FakeAgent(
        agent_id="recon-mock", name="Recon", endpoint="http://127.0.0.1:9201/"
    )
    directory = _FakeDirectory(by_skill={"recon/spider_robots": [recon]})

    canned = {
        "recon-mock": {
            "state": "completed",
            "summary": "ack",
            "structured_output": None,
            "message_id": None,
            "error": None,
        }
    }

    plan = [
        ("tool", "query_peer_agent", {
            "skill_query": "recon/spider_robots", "prompt": "status?",
        }),
        ("tool", "query_peer_agent", {
            "skill_query": "recon/spider_robots", "prompt": "any new contacts?",
        }),
        ("final", "done"),
    ]

    result, peer_sent, _ = asyncio.run(_send(
        "Brief.",
        plan=plan,
        directory=directory,
        monkeypatch=monkeypatch,
        canned_peer=canned,
    ))
    assert result["state"] == A2ATaskState.completed.value
    structured = result["structured_output"]
    assert len(structured["peer_calls"]) == 2
    assert all(p["status"] == "ok" for p in structured["peer_calls"])
    # Two A2A sends, but only one directory.search_agents call (cached).
    assert len(peer_sent) == 2
    assert directory.calls == ["recon/spider_robots"]


def test_final_message_propagates_to_a2a_message_text(monkeypatch) -> None:
    directory = _FakeDirectory()
    plan = [
        ("final",
         "SITUATIONAL AWARENESS: stand-down posture confirmed.\n"
         "INFORMATION GAPS: none."),
    ]

    result, _, _ = asyncio.run(_send(
        "Quick brief.",
        plan=plan,
        directory=directory,
        monkeypatch=monkeypatch,
    ))
    structured = result["structured_output"]
    assert structured["agent"]["final_message"].startswith("SITUATIONAL AWARENESS")

    # The A2A `complete()` message text mirrors the brief, so PI / users
    # see the brief without having to dig into the artifact.
    last_msg = result["messages"][-1] if result.get("messages") else None
    assert last_msg is not None
    text = next(
        (p["text"] for p in last_msg["parts"] if p.get("kind") == "text"), ""
    )
    assert "SITUATIONAL AWARENESS" in text


def test_unknown_tool_returns_error_in_trace(monkeypatch) -> None:
    directory = _FakeDirectory()
    plan = [
        ("tool", "consult_oracle", {"q": "what is the meaning"}),
        ("final", "Tool not available; abandoning."),
    ]
    result, _, _ = asyncio.run(_send(
        "Brief.",
        plan=plan,
        directory=directory,
        monkeypatch=monkeypatch,
    ))
    structured = result["structured_output"]
    tool_calls = structured["agent"]["tool_calls"]
    assert tool_calls[0]["name"] == "consult_oracle"
    assert tool_calls[0]["error"] is not None
    assert "unknown tool" in tool_calls[0]["error"]
