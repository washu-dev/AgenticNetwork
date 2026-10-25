"""H1: only a conversation's owner can answer its pending action.

A chat "yes" or "no" resumes or cancels a pending write confirmation or credential
consent, and the resumed call is built from the replying user's credentials and
identity. So the reply must pass the same ownership check as any other message into
the conversation (ContextOwnershipError, which the HTTP layer maps to 403), and the
owner's own reply still works.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import httpx
import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentSkill

from chat_app.store import conversation_store, pending_store
from chat_app.planner.config import PlannerConfig
from chat_app.planner.credential_gate import InMemoryCredentialGate
from agentnet.directory.card_oasf import (
    REQUIRED_CREDENTIALS_EXTENSION_URI,
    load_agent_card,
)
from chat_app.planner.llm_client import ScriptedLLMClient, ToolCall
from chat_app.planner.models import A2ATaskState, Channel
from chat_app.planner.runtime import ContextOwnershipError, PlannerRuntime
from chat_app.server import create_app
from chat_app.planner.memory_store import MemoryStore

from chat_app.tests.test_runtime import EMAIL_CARD, SpyA2A, SpyDirectory, _config

OWNER = "owner@example.test"
OTHER = "other@example.test"
OWNER_META = {"auth_user_id": "owner-id", "credentials": {"openai_api_key": {"api_key": "sk-owner"}}}
OTHER_META = {"auth_user_id": "other-id", "credentials": {"openai_api_key": {"api_key": "sk-other"}}}

SEND_EMAIL = [
    ToolCall(tool_name="list_agents", arguments={}),
    ToolCall(
        tool_name="call_agent",
        arguments={
            "agent_id": "email-agent",
            "skill": "email/send",
            "task_input": {"to": ["someone@example.test"], "subject": "s", "body": "b"},
        },
    ),
]
CALL_CRED_BOT = [
    ToolCall(tool_name="list_agents", arguments={}),
    ToolCall(tool_name="call_agent", arguments={"agent_id": "cred-bot", "skill": "credbot/handle", "task_input": {}}),
]


def _write_runtime() -> tuple[PlannerRuntime, SpyA2A]:
    """Strict policy, so email/send waits for a yes. The email agent is a legacy
    unscoped agent here, so the resumed call carries the replying user's credentials."""
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    config = PlannerConfig(
        llm_enabled=False,
        llm_model="mock-llm",
        max_steps=8,
        fake_ads=True,
        policy_mode="strict",
        unscoped_credential_agents=["email-agent"],
    )
    a2a = SpyA2A()
    runtime = PlannerRuntime(
        config=config,
        directory_client=directory,
        a2a_client=a2a,
        state_store=MemoryStore(),
        llm_client=ScriptedLLMClient(list(SEND_EMAIL)),
    )
    return runtime, a2a


def _consent_runtime(gate: InMemoryCredentialGate) -> tuple[PlannerRuntime, SpyA2A]:
    """An agent that requires an OpenAI key the owner has stored but not granted."""
    ext = AgentExtension(
        uri=REQUIRED_CREDENTIALS_EXTENSION_URI,
        description="creds",
        required=False,
        params={"credentials": [{"type": "openai_api_key", "label": "OpenAI key", "required": True}]},
    )
    card = AgentCard(
        name="Cred Bot", description="needs a key", url="http://10.0.0.9:19110/", version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, extensions=[ext]),
        defaultInputModes=["text"], defaultOutputModes=["text"],
        skills=[AgentSkill(id="credbot/handle", name="handle", description="x", tags=["credbot/handle"])],
    )
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(card)
    a2a = SpyA2A()
    runtime = PlannerRuntime(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        state_store=MemoryStore(),
        llm_client=ScriptedLLMClient(list(CALL_CRED_BOT)),
        credential_gate=gate,
    )
    return runtime, a2a


async def _say(runtime, user, text, context_id=None, metadata=None):
    return await runtime.handle_message(
        user_id=user, channel=Channel.api, text=text, context_id=context_id, metadata=dict(metadata or {}),
    )


class PendingRows(dict):
    """context_id -> (owner, payload), plus ``claimed``: context_id -> (claim time, claim id),
    and ``released``: context_id -> the claim ids given back for the current prompt."""

    def __init__(self) -> None:
        super().__init__()
        self.claimed: dict[str, list] = {}
        self.released: dict[str, set[str]] = {}

    def waiting(self, context_id: str) -> bool:
        """A row is there and no answer has claimed it."""
        return context_id in self and context_id not in self.claimed

    def age_claim(self, context_id: str, seconds: float) -> None:
        self.claimed[context_id][0] -= seconds

    def held_by(self, context_id: str, claim_id: str) -> bool:
        return context_id in self.claimed and self.claimed[context_id][1] == claim_id


@pytest.fixture
def persisted(monkeypatch):
    """A Postgres stand-in shared by every runtime in a test, so a fresh runtime acts as
    another planner process: conversations (owner, history) and pending rows (owner,
    payload, claim), following the statements in pending_store."""
    conversations: dict[str, tuple[str, list[str]]] = {}
    pending_rows = PendingRows()

    async def conversation_owner(context_id):
        entry = conversations.get(context_id)
        return entry[0] if entry else None

    async def load_history(context_id):
        entry = conversations.get(context_id)
        return list(entry[1]) if entry else None

    async def save_pending(context_id, owner_email, payload):
        pending_rows[context_id] = (owner_email, dict(payload))
        pending_rows.claimed.pop(context_id, None)
        pending_rows.released.pop(context_id, None)

    async def get_pending(context_id):
        entry = pending_rows.get(context_id)
        claim = pending_rows.claimed.get(context_id)
        if entry is None or (claim and time.monotonic() - claim[0] > pending_store.CLAIM_TTL_SECONDS):
            return None
        return dict(entry[1]), claim is not None

    async def pending_owner(context_id):
        entry = pending_rows.get(context_id)
        return entry[0] if entry else None

    async def clear_pending(context_id):
        pending_rows.pop(context_id, None)
        pending_rows.claimed.pop(context_id, None)
        pending_rows.released.pop(context_id, None)

    async def claim_pending(context_id, payload, claim_id):
        if not pending_rows.waiting(context_id) or pending_rows[context_id][1] != payload:
            return False
        if claim_id in pending_rows.released.get(context_id, ()):
            return False
        pending_rows.claimed[context_id] = [time.monotonic(), claim_id]
        return True

    async def refresh_pending(context_id, claim_id):
        if pending_rows.held_by(context_id, claim_id):
            pending_rows.claimed[context_id][0] = time.monotonic()

    async def release_pending(context_id, claim_id):
        if context_id in pending_rows:
            pending_rows.released.setdefault(context_id, set()).add(claim_id)
        if pending_rows.held_by(context_id, claim_id):
            del pending_rows.claimed[context_id]

    async def finish_pending(context_id, claim_id):
        if pending_rows.held_by(context_id, claim_id):
            await clear_pending(context_id)

    monkeypatch.setattr(conversation_store, "owner", conversation_owner)
    monkeypatch.setattr(conversation_store, "load_history", load_history)
    monkeypatch.setattr(pending_store, "enabled", lambda: True)
    monkeypatch.setattr(pending_store, "save", save_pending)
    monkeypatch.setattr(pending_store, "get", get_pending)
    monkeypatch.setattr(pending_store, "owner", pending_owner)
    monkeypatch.setattr(pending_store, "clear", clear_pending)
    monkeypatch.setattr(pending_store, "claim", claim_pending)
    monkeypatch.setattr(pending_store, "refresh", refresh_pending)
    monkeypatch.setattr(pending_store, "release", release_pending)
    monkeypatch.setattr(pending_store, "finish", finish_pending)
    return conversations, pending_rows


def test_another_users_yes_or_no_leaves_a_pending_confirmation_alone() -> None:
    async def go():
        runtime, a2a = _write_runtime()
        first = await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)
        assert first.confirmation_required, first.text
        cid = first.context_id
        pending = runtime.store.get_pending_confirmation(cid)
        history = list(runtime.store.get_context(cid).conversation_history)

        for reply in ("yes", "no"):
            with pytest.raises(ContextOwnershipError):
                await _say(runtime, OTHER, reply, cid, OTHER_META)

        assert a2a.requests == []
        assert runtime.store.get_pending_confirmation(cid) is pending
        assert runtime.store.get_context(cid).conversation_history == history

    asyncio.run(go())


def test_another_users_yes_or_no_leaves_a_credential_consent_alone() -> None:
    async def go():
        gate = InMemoryCredentialGate()
        runtime, a2a = _consent_runtime(gate)
        cid = (await _say(runtime, OWNER, "do it", metadata=OWNER_META)).context_id
        pending = runtime.store.get_pending_confirmation(cid)
        assert pending and pending["kind"] == "credential_consent"

        for reply in ("yes", "no"):
            with pytest.raises(ContextOwnershipError):
                await _say(runtime, OTHER, reply, cid, OTHER_META)

        assert a2a.requests == []
        assert await gate.list_granted("other-id", "cred-bot") == set()
        assert await gate.list_granted("owner-id", "cred-bot") == set()
        assert runtime.store.get_pending_confirmation(cid) is pending

    asyncio.run(go())


def test_owner_yes_still_runs_the_confirmed_call_after_another_users_no() -> None:
    async def go():
        runtime, a2a = _write_runtime()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        with contextlib.suppress(ContextOwnershipError):
            await _say(runtime, OTHER, "no", cid, OTHER_META)

        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)

        assert done.state == A2ATaskState.completed, done.text
        assert a2a.calls == [("email-agent", "email/send")]
        request = a2a.requests[0]
        assert request.context_id == cid
        assert request.metadata["confirmed"] is True
        assert request.metadata["credentials"] == OWNER_META["credentials"]
        assert runtime.store.get_pending_confirmation(cid) is None

    asyncio.run(go())


def test_resumed_consent_uses_the_owners_credentials_and_records_the_owners_grant() -> None:
    async def go():
        gate = InMemoryCredentialGate()
        runtime, a2a = _consent_runtime(gate)
        cid = (await _say(runtime, OWNER, "do it", metadata=OWNER_META)).context_id
        with contextlib.suppress(ContextOwnershipError):
            await _say(runtime, OTHER, "yes", cid, OTHER_META)

        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)

        assert done.state == A2ATaskState.completed, done.text
        assert len(a2a.requests) == 1
        assert a2a.requests[0].context_id == cid
        assert a2a.requests[0].metadata["credentials"] == OWNER_META["credentials"]
        assert await gate.list_granted("owner-id", "cred-bot") == {"openai_api_key"}
        assert await gate.list_granted("other-id", "cred-bot") == set()

    asyncio.run(go())


def test_reply_on_another_process_is_checked_against_the_persisted_owner(persisted) -> None:
    conversations, pending_rows = persisted

    async def go():
        first_process, _ = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        # What the server persists after the turn.
        conversations[cid] = (OWNER, list(first_process.store.get_context(cid).conversation_history))
        second_process, a2a = _write_runtime()

        for reply in ("yes", "no"):
            with pytest.raises(ContextOwnershipError):
                await _say(second_process, OTHER, reply, cid, OTHER_META)
        assert a2a.requests == [] and pending_rows.waiting(cid)

        done = await _say(second_process, OWNER, "yes", cid, OWNER_META)
        assert done.state == A2ATaskState.completed, done.text
        assert a2a.requests[0].metadata["credentials"] == OWNER_META["credentials"]
        assert cid not in pending_rows

    asyncio.run(go())


def test_pending_row_guards_a_conversation_whose_history_was_never_saved(persisted) -> None:
    conversations, pending_rows = persisted

    async def go():
        first_process, _ = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        # The pending row is written during the turn; the history save after it failed.
        assert pending_rows[cid][0] == OWNER and cid not in conversations
        second_process, a2a = _write_runtime()

        for text in ("yes", "no", "what is waiting here?"):
            with pytest.raises(ContextOwnershipError):
                await _say(second_process, OTHER, text, cid, OTHER_META)

        assert a2a.requests == [] and pending_rows.waiting(cid)
        assert cid not in second_process.store.contexts

    asyncio.run(go())


def test_owner_yes_on_another_process_does_not_leave_a_stale_copy_to_run_again(persisted) -> None:
    """Once the owner's yes ran the action on the second process, the first process must
    not run it again from its in-memory copy when the owner later says "ok" there."""
    conversations, pending_rows = persisted

    async def go():
        first_process, first_a2a = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        conversations[cid] = (OWNER, list(first_process.store.get_context(cid).conversation_history))
        second_process, second_a2a = _write_runtime()

        done = await _say(second_process, OWNER, "yes", cid, OWNER_META)
        assert done.state == A2ATaskState.completed, done.text
        assert cid not in pending_rows
        assert first_process.store.get_pending_confirmation(cid) is not None  # the stale copy

        await _say(first_process, OWNER, "ok", cid, OWNER_META)

        assert second_a2a.calls == [("email-agent", "email/send")]
        assert first_a2a.calls == []
        assert first_process.store.get_pending_confirmation(cid) is None

    asyncio.run(go())


def _email_app() -> tuple[object, SpyA2A]:
    """The HTTP app of another planner process (empty memory), for the confirm button."""
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SpyA2A()
    app = create_app(
        config=_config("strict"),
        directory_client=directory,
        a2a_client=a2a,
        state_store=MemoryStore(),
        llm_client=ScriptedLLMClient([]),
    )
    return app, a2a


async def _press(app, user, context_id, confirmed):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(
            "/confirmations", json={"context_id": context_id, "confirmed": confirmed}, headers={"mail": user},
        )


def test_confirm_button_checks_the_pending_rows_owner(persisted, monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conversations, pending_rows = persisted

    async def go():
        first_process, _ = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        assert cid not in conversations  # only the pending row records the owner
        app, a2a = _email_app()

        for confirmed in (False, True):
            resp = await _press(app, OTHER, cid, confirmed)
            assert resp.status_code == 403, resp.text

        assert a2a.requests == [] and pending_rows.waiting(cid)

    asyncio.run(go())


def test_confirm_button_resumes_an_action_raised_on_another_process(persisted, monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conversations, pending_rows = persisted

    async def go():
        first_process, _ = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        conversations[cid] = (OWNER, list(first_process.store.get_context(cid).conversation_history))
        app, a2a = _email_app()

        refused = await _press(app, OTHER, cid, True)
        resp = await _press(app, OWNER, cid, True)

        assert refused.status_code == 403, refused.text
        assert resp.status_code == 200 and resp.json()["state"] == "completed", resp.text
        assert a2a.calls == [("email-agent", "email/send")]
        assert cid not in pending_rows

    asyncio.run(go())


def test_pending_store_owner_reads_the_rows_owner(monkeypatch) -> None:
    queries: list[str] = []

    class Conn:
        async def fetchrow(self, query, context_id):
            queries.append(query)
            return {"owner_email": OWNER} if context_id == "c1" else None

    class Pool:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield Conn()

    async def get_pool():
        return Pool()

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert asyncio.run(pending_store.owner("c1")) is None  # no DB: nothing is persisted
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    monkeypatch.setattr(pending_store, "get_pool", get_pool)
    assert asyncio.run(pending_store.owner("c1")) == OWNER
    assert asyncio.run(pending_store.owner("c2")) is None
    assert queries and all("FROM pending_confirmations" in q for q in queries)
