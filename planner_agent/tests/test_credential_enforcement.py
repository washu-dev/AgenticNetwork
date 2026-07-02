"""P4c — credential enforcement: block / consent / scoped injection / carve-out."""

from __future__ import annotations

import asyncio

from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentSkill

from planner_agent.main_agent.credential_gate import InMemoryCredentialGate
from planner_agent.main_agent.directory.card_oasf_converter import (
    REQUIRED_CREDENTIALS_EXTENSION_URI,
)
from planner_agent.main_agent.models import Channel
from planner_agent.main_agent.runtime import MainAgentRuntime
from planner_agent.main_agent.state.store import InMemoryStateStore
from planner_agent.main_agent.llm_client import ScriptedLLMClient, ToolCall

from planner_agent.tests.test_main_agent_runtime import SpyA2A, SpyDirectory, _config

USER = "user-uuid-1"
OPENAI_CRED = {"openai_api_key": {"api_key": "sk-secret"}}


# ── namespace + gate + schema (unit) ─────────────────────────────────────────

def test_router_allows_catalog_types_and_custom_rejects_legacy():
    from planner_agent.main_agent.credentials_router import _is_allowed_provider
    assert _is_allowed_provider("openai_api_key")
    assert _is_allowed_provider("custom:weather")
    assert not _is_allowed_provider("openai")       # legacy value no longer accepted on write
    assert not _is_allowed_provider("bogus")


def test_schema_has_migration_and_grants_table():
    from planner_agent.main_agent.db import _SCHEMA_SQL
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


def _runtime(card, gate, script):
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(card)
    a2a = SpyA2A()
    rt = MainAgentRuntime(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        state_store=InMemoryStateStore(),
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
        res2 = await _msg(rt, text="yes", context_id=res1.context_id, metadata=meta)
        assert len(a2a.requests) == 1                   # now dispatched
        assert a2a.requests[0].metadata.get("credentials") == OPENAI_CRED  # scoped to the one cred
        assert await gate.list_granted(USER, "cred-bot") == {"openai_api_key"}
    asyncio.run(go())


# ── button path: /confirmations calls _resume_credential_consent directly ────

def test_confirmations_button_resume_grants_and_scopes():
    async def go():
        card = _cred_card([{"type": "openai_api_key", "label": "OpenAI key", "required": True}])
        gate = InMemoryCredentialGate()
        rt, a2a = _runtime(card, gate, [_LIST, _CALL])
        meta = {"auth_user_id": USER, "credentials": OPENAI_CRED}
        res1 = await _msg(rt, metadata=meta)
        assert a2a.requests == []
        # Simulate the "Allow" button: server injects creds into metadata, looks up
        # the pending, and calls _resume_credential_consent (server.py /confirmations).
        pending = rt.store.get_pending_confirmation(res1.context_id)
        assert pending and pending.get("kind") == "credential_consent"
        await rt._resume_credential_consent(res1.context_id, "u", Channel.api, meta, pending)
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


# ── carve-out: agent declares no creds → full unscoped dict passes through ───

def test_no_declaration_passes_full_credentials():
    async def go():
        card = AgentCard(
            name="Plain Bot", description="d", url="http://10.0.0.9:19111/", version="1.0.0",
            capabilities=AgentCapabilities(streaming=True),
            defaultInputModes=["text"], defaultOutputModes=["text"],
            skills=[AgentSkill(id="plain/handle", name="handle", description="x", tags=["plain/handle"])],
        )
        gate = InMemoryCredentialGate()
        directory = SpyDirectory(fake_ads=True)
        directory.publish_agent_card(card)
        a2a = SpyA2A()
        rt = MainAgentRuntime(
            config=_config(), directory_client=directory, a2a_client=a2a,
            state_store=InMemoryStateStore(),
            llm_client=ScriptedLLMClient([
                _LIST,
                ToolCall(tool_name="call_agent", arguments={"agent_id": "plain-bot", "skill": "plain/handle", "task_input": {}}),
                _FINAL,
            ]),
            credential_gate=gate,
        )
        full = {"openai_api_key": {"api_key": "x"}, "google_oauth": {"access_token": "t"}}
        await _msg(rt, metadata={"auth_user_id": USER, "credentials": full})
        assert a2a.requests[0].metadata.get("credentials") == full   # unchanged (email/geo compat)
    asyncio.run(go())


# ── no gate configured → pass-through (P4 inert without a DB) ────────────────

def test_no_gate_passes_through():
    async def go():
        card = _cred_card([{"type": "openai_api_key", "label": "OpenAI key", "required": True}])
        rt, a2a = _runtime(card, None, [_LIST, _CALL, _FINAL])  # gate=None
        await _msg(rt, metadata={"auth_user_id": USER, "credentials": OPENAI_CRED})
        assert len(a2a.requests) == 1
        assert a2a.requests[0].metadata.get("credentials") == OPENAI_CRED
    asyncio.run(go())
