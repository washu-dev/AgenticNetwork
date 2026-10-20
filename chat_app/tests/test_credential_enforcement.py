"""P4c — credential enforcement: block / consent / scoped injection / legacy allowlist."""

from __future__ import annotations

import asyncio
import logging

from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentSkill

from chat_app.planner.config import load_config
from chat_app.planner.credential_gate import InMemoryCredentialGate
from agentnet.directory.card_oasf import (
    REQUIRED_CREDENTIALS_EXTENSION_URI,
)
from chat_app.planner.models import Channel
from chat_app.planner.runtime import PlannerRuntime
from chat_app.planner.memory_store import MemoryStore
from chat_app.planner.llm_client import ScriptedLLMClient, ToolCall

from chat_app.tests.test_runtime import SpyA2A, SpyDirectory, _config

USER = "user-uuid-1"
OPENAI_CRED = {"openai_api_key": {"api_key": "sk-secret"}}


# ── namespace + gate + schema (unit) ─────────────────────────────────────────

def test_router_allows_catalog_types_and_custom_rejects_legacy():
    from chat_app.credentials_router import _is_allowed_provider
    assert _is_allowed_provider("openai_api_key")
    assert _is_allowed_provider("custom:weather")
    assert not _is_allowed_provider("openai")       # legacy value no longer accepted on write
    assert not _is_allowed_provider("bogus")


def test_schema_has_migration_and_grants_table():
    from chat_app.store.db import _SCHEMA_SQL
    assert "agent_credential_grants" in _SCHEMA_SQL
    assert "SET provider = 'openai_api_key'" in _SCHEMA_SQL   # legacy self-heal


def test_in_memory_gate_add_and_list():
    async def go():
        gate = InMemoryCredentialGate()
        assert await gate.list_granted(USER, "a1") == set()
        await gate.add_grant(USER, "a1", "openai_api_key")
        await gate.add_grant(USER, "a1", "openai_api_key")  # idempotent
        assert await gate.list_granted(USER, "a1") == {"openai_api_key"}
        assert await gate.list_granted(USER, "a2") == set()  # scoped per agent
    asyncio.run(go())


def _cred_card(credentials, name="Cred Bot", required=True) -> AgentCard:
    ext = AgentExtension(
        uri=REQUIRED_CREDENTIALS_EXTENSION_URI, description="creds", required=False,
        params={"credentials": credentials},
    )
    return AgentCard(
        name=name, description="needs a key", url="http://10.0.0.9:19110/", version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, extensions=[ext]),
        defaultInputModes=["text"], defaultOutputModes=["text"],
        skills=[AgentSkill(id="credbot/handle", name="handle", description="x", tags=["credbot/handle"])],
    )


def _runtime(card, gate, script, config=None):
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(card)
    a2a = SpyA2A()
    rt = PlannerRuntime(
        config=config or _config(),
        directory_client=directory,
        a2a_client=a2a,
        state_store=MemoryStore(),
        llm_client=ScriptedLLMClient(script),
        credential_gate=gate,
    )
    return rt, a2a


_CALL = ToolCall(tool_name="call_agent", arguments={"agent_id": "cred-bot", "skill": "credbot/handle", "task_input": {}})
_LIST = ToolCall(tool_name="list_agents", arguments={})
_FINAL = ToolCall(tool_name="final_answer", arguments={"text": "done"})


async def _msg(rt, text="do it", context_id=None, metadata=None):
    return await rt.handle_message(
        user_id="u", channel=Channel.api, text=text, context_id=context_id, metadata=metadata or {},
    )


# ── required + missing → block, no dispatch ──────────────────────────────────

def test_required_missing_blocks():
    async def go():
        card = _cred_card([{"type": "openai_api_key", "label": "OpenAI key", "required": True}])
        rt, a2a = _runtime(card, InMemoryCredentialGate(), [_LIST, _CALL])
        # key stored but auth present; here nothing stored
        res = await _msg(rt, metadata={"auth_user_id": USER, "credentials": {}})
        assert a2a.requests == []                       # never dispatched
        assert "Settings" in res.text and "OpenAI key" in res.text
    asyncio.run(go())


# ── required + stored + not granted → consent, then grant on "yes" → dispatch ─

def test_consent_then_grant_dispatches_scoped():
    async def go():
        card = _cred_card([{"type": "openai_api_key", "label": "OpenAI key", "required": True}])
        gate = InMemoryCredentialGate()
        rt, a2a = _runtime(card, gate, [_LIST, _CALL])
        meta = {"auth_user_id": USER, "credentials": OPENAI_CRED}
        res1 = await _msg(rt, metadata=meta)
        assert a2a.requests == []                       # consent first, no dispatch
        assert "yes" in res1.text.lower()
        # user approves
        await _msg(rt, text="yes", context_id=res1.context_id, metadata=meta)
        assert len(a2a.requests) == 1                   # now dispatched
        assert a2a.requests[0].metadata.get("credentials") == OPENAI_CRED  # scoped to the one cred
        assert await gate.list_granted(USER, "cred-bot") == {"openai_api_key"}
    asyncio.run(go())


# ── button path: /confirmations calls _answer_pending directly ───────────────

def test_confirmations_button_resume_grants_and_scopes():
    async def go():
        card = _cred_card([{"type": "openai_api_key", "label": "OpenAI key", "required": True}])
        gate = InMemoryCredentialGate()
        rt, a2a = _runtime(card, gate, [_LIST, _CALL])
        meta = {"auth_user_id": USER, "credentials": OPENAI_CRED}
        res1 = await _msg(rt, metadata=meta)
        assert a2a.requests == []
        # Simulate the "Allow" button: server injects creds into metadata and calls
        # _answer_pending, which claims the pending consent (server.py /confirmations).
        pending = rt.store.get_pending_confirmation(res1.context_id)
        assert pending and pending.get("kind") == "credential_consent"
        await rt._answer_pending(res1.context_id, Channel.api, meta, confirmed=True)
        assert rt.store.get_pending_confirmation(res1.context_id) is None
        assert len(a2a.requests) == 1
        assert a2a.requests[0].metadata.get("credentials") == OPENAI_CRED
        assert await gate.list_granted(USER, "cred-bot") == {"openai_api_key"}
    asyncio.run(go())


# ── already granted → dispatch immediately with scoped creds ─────────────────

def test_granted_dispatches_scoped_only():
    async def go():
        # agent declares openai; user also has an unrelated cred that must NOT leak
        card = _cred_card([{"type": "openai_api_key", "label": "OpenAI key", "required": True}])
        gate = InMemoryCredentialGate({(USER, "cred-bot", "openai_api_key")})
        rt, a2a = _runtime(card, gate, [_LIST, _CALL, _FINAL])
        meta = {"auth_user_id": USER, "credentials": {**OPENAI_CRED, "brave_api_key": {"api_key": "b"}}}
        await _msg(rt, metadata=meta)
        assert len(a2a.requests) == 1
        assert a2a.requests[0].metadata.get("credentials") == OPENAI_CRED   # brave scoped out
    asyncio.run(go())


# ── optional + missing → proceeds (no block, no inject) ──────────────────────

def test_optional_missing_proceeds():
    async def go():
        card = _cred_card([{"type": "openai_api_key", "label": "OpenAI key", "required": False}])
        rt, a2a = _runtime(card, InMemoryCredentialGate(), [_LIST, _CALL, _FINAL])
        await _msg(rt, metadata={"auth_user_id": USER, "credentials": {}})
        assert len(a2a.requests) == 1
        assert "credentials" not in a2a.requests[0].metadata   # nothing injected
    asyncio.run(go())


# ── agent declares no creds → nothing injected (no email/geo carve-out) ─────

FULL_BUNDLE = {"openai_api_key": {"api_key": "x"}, "google_oauth": {"access_token": "t"}}


def _plain_card() -> AgentCard:
    return AgentCard(
        name="Plain Bot", description="d", url="http://10.0.0.9:19111/", version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        defaultInputModes=["text"], defaultOutputModes=["text"],
        skills=[AgentSkill(id="plain/handle", name="handle", description="x", tags=["plain/handle"])],
    )


_PLAIN_CALL = ToolCall(tool_name="call_agent", arguments={"agent_id": "plain-bot", "skill": "plain/handle", "task_input": {}})


def test_undeclared_agent_gets_no_credentials():
    async def go():
        rt, a2a = _runtime(_plain_card(), InMemoryCredentialGate(), [_LIST, _PLAIN_CALL, _FINAL])
        await _msg(rt, metadata={"auth_user_id": USER, "credentials": FULL_BUNDLE})
        assert len(a2a.requests) == 1
        assert "credentials" not in a2a.requests[0].metadata
        # the confirm/consent resume paths scope through the same method
        agent = rt.directory.resolve_agent("plain-bot")
        assert await rt._scope_credentials(agent, FULL_BUNDLE, USER) == {}
        assert await rt._scope_credentials(agent, FULL_BUNDLE, None) == {}
    asyncio.run(go())


def test_allowlisted_legacy_agent_gets_full_bundle(caplog):
    async def go():
        env = {"PLANNER_LLM_ENABLED": "false", "PLANNER_UNSCOPED_CREDENTIAL_AGENTS": " plain-bot , email-agent "}
        allow = load_config(env).unscoped_credential_agents
        assert allow == ["plain-bot", "email-agent"]
        config = _config().model_copy(update={"unscoped_credential_agents": allow})
        rt, a2a = _runtime(_plain_card(), InMemoryCredentialGate(), [_LIST, _PLAIN_CALL, _FINAL], config)
        await _msg(rt, metadata={"auth_user_id": USER, "credentials": FULL_BUNDLE})
        assert a2a.requests[0].metadata.get("credentials") == FULL_BUNDLE
    with caplog.at_level(logging.WARNING, logger="chat_app.planner.runtime"):
        asyncio.run(go())
    assert "credentials_unscoped_legacy_agent agent_id=plain-bot" in caplog.text


# ── no gate or no identity → only the declared types (never the whole bundle) ─

def test_no_gate_passes_only_declared_types():
    async def go():
        card = _cred_card([{"type": "openai_api_key", "label": "OpenAI key", "required": True}])
        rt, a2a = _runtime(card, None, [_LIST, _CALL, _FINAL])  # gate=None
        await _msg(rt, metadata={"auth_user_id": USER, "credentials": FULL_BUNDLE})
        assert len(a2a.requests) == 1
        assert a2a.requests[0].metadata.get("credentials") == {"openai_api_key": {"api_key": "x"}}
    asyncio.run(go())


def test_no_identity_passes_only_declared_types():
    async def go():
        card = _cred_card([{"type": "openai_api_key", "label": "OpenAI key", "required": True}])
        rt, a2a = _runtime(card, InMemoryCredentialGate(), [_LIST, _CALL, _FINAL])
        await _msg(rt, metadata={"credentials": FULL_BUNDLE})  # no auth_user_id
        assert len(a2a.requests) == 1
        assert a2a.requests[0].metadata.get("credentials") == {"openai_api_key": {"api_key": "x"}}
    asyncio.run(go())
