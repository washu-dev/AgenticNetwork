"""One answer per pending action.

Two answers to the same pending write confirmation or credential consent can arrive
together: a double click on the confirm button, a client retry, a chat "yes" racing the
button, or replies landing on two planner processes. Each answer used to read the action,
send the agent call and clear the action only afterwards, so both ran the write (the same
email sent twice). Now (MainAgentRuntime._answer_pending):

- within one process, one answer per conversation runs at a time; another gets
  ``answer_in_progress`` and leaves the action alone;
- an answer does what can fail first, then claims the action (one DELETE ... RETURNING
  with a database, a pop from the in-memory copy without one) right before the agent call,
  so across processes exactly one answer acts and the other gets ``no_pending``;
- a claim the database can't serve runs nothing (``pending_store_unavailable``).

The races hold the first answer inside its agent call (SlowA2A) while the second arrives,
which is when both used to run.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentSkill

from planner_agent.main_agent import pending_store
from planner_agent.main_agent.credential_gate import InMemoryCredentialGate
from planner_agent.main_agent.directory.card_oasf_converter import (
    REQUIRED_CREDENTIALS_EXTENSION_URI,
    load_agent_card,
)
from planner_agent.main_agent.llm_client import ScriptedLLMClient
from planner_agent.main_agent.models import A2ATaskState, Channel
from planner_agent.main_agent.runtime import MainAgentRuntime
from planner_agent.main_agent.server import create_app
from planner_agent.main_agent.state.store import InMemoryStateStore

from planner_agent.tests.test_confirmation_ownership import (  # noqa: F401 (persisted is a fixture)
    CALL_CRED_BOT, OWNER, OWNER_META, SEND_EMAIL, _say, _write_runtime, persisted,
)
from planner_agent.tests.test_main_agent_runtime import EMAIL_CARD, SpyA2A, SpyDirectory, _config


class SlowA2A(SpyA2A):
    """Holds every agent call until released (or 1 s passes), so a second answer can
    arrive while the first is still inside its call."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send_task(self, agent, request):
        self.entered.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.release.wait(), 1)
        return await super().send_task(agent, request)


def _email_app() -> tuple[object, SlowA2A]:
    """A planner whose email/send waits for a yes (strict policy)."""
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SlowA2A()
    app = create_app(
        config=_config("strict"),
        directory_client=directory,
        a2a_client=a2a,
        state_store=InMemoryStateStore(),
        llm_client=ScriptedLLMClient(list(SEND_EMAIL)),
    )
    return app, a2a


def _cred_bot_runtime(gate, script=CALL_CRED_BOT) -> tuple[MainAgentRuntime, SlowA2A]:
    """Permissive policy; Cred Bot requires an OpenAI key the owner has stored but not granted."""
    ext = AgentExtension(
        uri=REQUIRED_CREDENTIALS_EXTENSION_URI, description="creds", required=False,
        params={"credentials": [{"type": "openai_api_key", "label": "OpenAI key", "required": True}]},
    )
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(AgentCard(
        name="Cred Bot", description="needs a key", url="http://10.0.0.9:19110/", version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, extensions=[ext]),
        defaultInputModes=["text"], defaultOutputModes=["text"],
        skills=[AgentSkill(id="credbot/handle", name="handle", description="x", tags=["credbot/handle"])],
    ))
    a2a = SlowA2A()
    runtime = MainAgentRuntime(
        config=_config(), directory_client=directory, a2a_client=a2a,
        state_store=InMemoryStateStore(), llm_client=ScriptedLLMClient(list(script)),
        credential_gate=gate,
    )
    return runtime, a2a


async def _post(app, path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(path, json=body, headers={"mail": OWNER})


async def _ask_to_send(app) -> str:
    asked = await _post(app, "/messages", {"channel": "api", "text": "Send the email."})
    assert asked.status_code == 200 and asked.json()["confirmation_required"], asked.text
    return asked.json()["context_id"]


async def _ask_consent(runtime) -> str:
    cid = (await _say(runtime, OWNER, "do it", metadata=OWNER_META)).context_id
    assert runtime.store.get_pending_confirmation(cid)["kind"] == "credential_consent"
    return cid


async def _press(app, context_id, confirmed):
    return await _post(app, "/confirmations", {"context_id": context_id, "confirmed": confirmed})


def _answer(runtime, context_id, confirmed=True):
    return runtime._answer_pending(context_id, OWNER, Channel.api, dict(OWNER_META), confirmed=confirmed)


async def _while_first_runs(a2a: SlowA2A, first, second):
    """Start ``first``, wait until it is inside its agent call, run ``second`` to the end,
    then let ``first`` finish. Returns both results."""
    running = asyncio.create_task(first)
    await asyncio.wait_for(a2a.entered.wait(), 2)
    other = await second
    a2a.release.set()
    return await running, other


def _refused(answer, error: str) -> None:
    """``answer`` is an HTTP response or a TurnResult that acted on nothing."""
    if isinstance(answer, httpx.Response):
        assert answer.status_code == 200, answer.text
        state, errors, text = answer.json()["state"], answer.json()["errors"], answer.json()["text"]
    else:
        state, errors, text = answer.state.value, answer.errors, answer.text
    assert state == "failed" and errors == [error] and "Cancelled" not in text, (state, errors, text)


# ── Two answers in one process ────────────────────────────────────────────────


def test_second_confirm_press_while_the_first_runs_sends_nothing() -> None:
    async def go():
        app, a2a = _email_app()
        cid = await _ask_to_send(app)

        first, second = await _while_first_runs(a2a, _press(app, cid, True), _press(app, cid, True))

        assert first.status_code == 200 and first.json()["state"] == "completed", first.text
        _refused(second, "answer_in_progress")
        assert a2a.calls == [("email-agent", "email/send")]

    asyncio.run(go())


def test_chat_yes_while_the_confirm_press_runs_does_not_send_again() -> None:
    async def go():
        app, a2a = _email_app()
        cid = await _ask_to_send(app)
        yes = _post(app, "/messages", {"channel": "api", "text": "yes", "context_id": cid})

        first, second = await _while_first_runs(a2a, _press(app, cid, True), yes)

        assert first.json()["state"] == "completed", first.text
        _refused(second, "answer_in_progress")
        assert a2a.calls == [("email-agent", "email/send")]

    asyncio.run(go())


def test_cancel_while_the_confirmed_call_runs_does_not_claim_a_cancel() -> None:
    async def go():
        app, a2a = _email_app()
        cid = await _ask_to_send(app)

        first, second = await _while_first_runs(a2a, _press(app, cid, True), _press(app, cid, False))

        assert first.json()["state"] == "completed", first.text
        _refused(second, "answer_in_progress")
        assert a2a.calls == [("email-agent", "email/send")]

    asyncio.run(go())


def test_allow_pressed_while_the_consented_call_runs_dispatches_once() -> None:
    async def go():
        gate = InMemoryCredentialGate()
        runtime, a2a = _cred_bot_runtime(gate)
        cid = await _ask_consent(runtime)

        first, second = await _while_first_runs(a2a, _say(runtime, OWNER, "yes", cid, OWNER_META), _answer(runtime, cid))

        assert first.state == A2ATaskState.completed, first.text
        _refused(second, "answer_in_progress")
        assert len(a2a.requests) == 1
        assert await gate.list_granted("owner-id", "cred-bot") == {"openai_api_key"}

    asyncio.run(go())


def test_repeated_chat_yes_while_the_first_runs_starts_no_llm_turn() -> None:
    """The repeat used to find no pending action and start an LLM turn on "yes", and a
    model that called the agent again got through: the consent was granted by then."""
    async def go():
        gate = InMemoryCredentialGate()
        runtime, a2a = _cred_bot_runtime(gate, [*CALL_CRED_BOT, *CALL_CRED_BOT])
        cid = await _ask_consent(runtime)

        first, second = await _while_first_runs(
            a2a, _say(runtime, OWNER, "yes", cid, OWNER_META), _say(runtime, OWNER, "yes", cid, OWNER_META),
        )

        assert first.state == A2ATaskState.completed, first.text
        _refused(second, "answer_in_progress")
        assert len(a2a.requests) == 1 and a2a.requests[0].user_goal == "do it"
        assert cid not in runtime._answering

    asyncio.run(go())


def test_repeated_yes_during_the_claims_database_round_trip_starts_no_llm_turn(persisted, monkeypatch) -> None:
    """With a database the claim is a round trip: the row is gone before the first answer
    resumes. A repeat in that gap finds no row and must still not start an LLM turn."""
    conversations, pending_rows = persisted
    deleting = pending_store.take
    deleted = asyncio.Event()

    async def slow_take(context_id):
        claimed = await deleting(context_id)
        deleted.set()
        await asyncio.sleep(0.05)  # the reply is still on its way back
        return claimed

    monkeypatch.setattr(pending_store, "take", slow_take)

    async def go():
        gate = InMemoryCredentialGate()
        runtime, a2a = _cred_bot_runtime(gate, [*CALL_CRED_BOT, *CALL_CRED_BOT])
        a2a.release.set()
        cid = await _ask_consent(runtime)

        first = asyncio.create_task(_say(runtime, OWNER, "yes", cid, OWNER_META))
        await asyncio.wait_for(deleted.wait(), 2)
        assert cid not in pending_rows
        second = await _say(runtime, OWNER, "yes", cid, OWNER_META)

        _refused(second, "answer_in_progress")
        assert (await first).state == A2ATaskState.completed
        assert len(a2a.requests) == 1

    asyncio.run(go())


# ── Answers on two processes (a shared database stand-in) ─────────────────────


def test_answers_on_two_processes_at_once_run_it_once(persisted, monkeypatch) -> None:
    """Both processes read the pending row before either claims it; the claim decides."""
    conversations, pending_rows = persisted
    deleting = pending_store.take

    async def yielding_take(context_id):
        await asyncio.sleep(0)  # let the other process read the row first
        return await deleting(context_id)

    monkeypatch.setattr(pending_store, "take", yielding_take)

    async def go():
        first_process, first_a2a = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        conversations[cid] = (OWNER, list(first_process.store.get_context(cid).conversation_history))
        second_process, second_a2a = _write_runtime()
        await second_process._owned_context(OWNER, Channel.api, cid)  # rehydrates the conversation

        answers = await asyncio.gather(_answer(first_process, cid), _answer(second_process, cid))

        assert sorted(a.state.value for a in answers) == ["completed", "failed"], answers
        _refused(next(a for a in answers if a.state == A2ATaskState.failed), "no_pending")
        assert len(first_a2a.calls) + len(second_a2a.calls) == 1
        assert cid not in pending_rows

    asyncio.run(go())


def test_yes_on_another_process_while_the_press_runs_does_not_send_again(persisted, monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    conversations, pending_rows = persisted

    async def go():
        first_process, first_a2a = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        conversations[cid] = (OWNER, list(first_process.store.get_context(cid).conversation_history))
        app, second_a2a = _email_app()  # another process, with the confirm button

        pressed, _ = await _while_first_runs(
            second_a2a, _press(app, cid, True), _say(first_process, OWNER, "yes", cid, OWNER_META),
        )

        assert pressed.json()["state"] == "completed", pressed.text
        assert second_a2a.calls == [("email-agent", "email/send")] and first_a2a.calls == []
        assert cid not in pending_rows
        assert first_process.store.get_pending_confirmation(cid) is None

    asyncio.run(go())


def test_with_a_database_the_row_decides_not_this_processs_copy(persisted) -> None:
    """The process that raised the prompt still holds a copy after another process
    claimed the row; its own answer must find nothing rather than run that copy."""
    conversations, pending_rows = persisted

    async def go():
        first_process, first_a2a = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        conversations[cid] = (OWNER, list(first_process.store.get_context(cid).conversation_history))
        second_process, second_a2a = _write_runtime()
        await second_process._owned_context(OWNER, Channel.api, cid)

        done = await _answer(second_process, cid)
        assert done.state == A2ATaskState.completed, done.text
        assert first_process.store.get_pending_confirmation(cid) is not None  # the stale copy

        _refused(await _answer(first_process, cid), "no_pending")
        assert second_a2a.calls == [("email-agent", "email/send")] and first_a2a.calls == []
        assert first_process.store.get_pending_confirmation(cid) is None

    asyncio.run(go())


def test_confirm_on_a_process_without_the_conversation_leaves_the_action_pending(persisted, monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    conversations, pending_rows = persisted

    async def go():
        first_process, first_a2a = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        assert cid not in conversations  # the history save failed; only the row knows the owner
        app, second_a2a = _email_app()

        refused = await _press(app, cid, True)

        assert refused.status_code == 200 and refused.json()["errors"] == ["unknown_context"], refused.text
        assert second_a2a.calls == [] and cid in pending_rows
        done = await _answer(first_process, cid)
        assert done.state == A2ATaskState.completed, done.text
        assert first_a2a.calls == [("email-agent", "email/send")] and cid not in pending_rows

    asyncio.run(go())


def test_a_claim_the_database_cant_serve_runs_nothing_and_keeps_the_action(persisted, monkeypatch) -> None:
    """Running this process's copy when the DELETE fails would leave the row behind for a
    later "yes", here or on another process, to run the write again, or to run it after the
    owner was told it was cancelled. So nothing runs and the owner answers again."""
    conversations, pending_rows = persisted
    working_take = pending_store.take
    database = {"up": False}

    async def flaky_take(context_id):
        if not database["up"]:
            raise OSError("connection reset")
        return await working_take(context_id)

    monkeypatch.setattr(pending_store, "take", flaky_take)

    async def go():
        runtime, a2a = _write_runtime()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id

        for reply in ("yes", "no"):
            _refused(await _say(runtime, OWNER, reply, cid, OWNER_META), "pending_store_unavailable")
        assert a2a.calls == [] and cid in pending_rows
        assert runtime.store.get_pending_confirmation(cid) is not None

        database["up"] = True
        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)
        assert done.state == A2ATaskState.completed, done.text
        await _say(runtime, OWNER, "ok", cid, OWNER_META)

        assert a2a.calls == [("email-agent", "email/send")] and cid not in pending_rows

    asyncio.run(go())


def test_pending_store_take_deletes_and_returns_the_row(monkeypatch) -> None:
    queries: list[str] = []
    rows = {"c1": '{"skill": "email/send"}'}  # asyncpg hands JSONB back as a str

    class Conn:
        async def fetchrow(self, query, context_id):
            queries.append(query)
            payload = rows.pop(context_id, None)
            return {"payload": payload} if payload is not None else None

    class Pool:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield Conn()

    async def get_pool():
        return Pool()

    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    assert asyncio.run(pending_store.take("c1")) is None and queries == []  # no DB: a no-op
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.setattr(pending_store, "get_pool", get_pool)

    assert asyncio.run(pending_store.take("c1")) == {"skill": "email/send"}
    assert asyncio.run(pending_store.take("c1")) is None
    assert all(
        q.startswith("DELETE FROM pending_confirmations") and q.endswith("RETURNING payload") for q in queries
    ), queries


# ── What an answer acts on ────────────────────────────────────────────────────


@pytest.mark.parametrize("confirmed", [True, False])
def test_answer_with_nothing_pending_runs_nothing(confirmed) -> None:
    async def go():
        app, a2a = _email_app()
        cid = await _ask_to_send(app)
        assert (await _press(app, cid, False)).json()["state"] == "canceled"

        _refused(await _press(app, cid, confirmed), "no_pending")
        assert a2a.calls == []

    asyncio.run(go())


def test_the_confirmed_call_sends_the_request_the_owner_approved() -> None:
    """A message after the prompt moves the conversation's current goal; the confirmed call
    still carries the request shown in the prompt."""
    async def go():
        runtime, a2a = _write_runtime()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        await _say(runtime, OWNER, "Who is it going to?", cid, OWNER_META)

        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)

        assert done.state == A2ATaskState.completed, done.text
        assert [r.user_goal for r in a2a.requests] == ["Send the email."]

    asyncio.run(go())


def test_the_consented_call_sends_the_request_the_owner_approved() -> None:
    async def go():
        runtime, a2a = _cred_bot_runtime(InMemoryCredentialGate())
        a2a.release.set()
        cid = await _ask_consent(runtime)
        await _say(runtime, OWNER, "What will it see?", cid, OWNER_META)

        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)

        assert done.state == A2ATaskState.completed, done.text
        assert [r.user_goal for r in a2a.requests] == ["do it"]

    asyncio.run(go())


def test_an_error_before_the_claim_leaves_the_action_pending() -> None:
    """Credential lookups run before the claim, so their failure leaves nothing half done."""
    class FlakyGate(InMemoryCredentialGate):
        armed = False

        async def list_granted(self, user_id, agent_id):
            if self.armed:
                self.armed = False
                raise OSError("credential store unreachable")
            return await super().list_granted(user_id, agent_id)

    async def go():
        gate = FlakyGate()
        runtime, a2a = _cred_bot_runtime(gate)
        a2a.release.set()
        cid = await _ask_consent(runtime)
        gate.armed = True

        with pytest.raises(OSError):
            await _say(runtime, OWNER, "yes", cid, OWNER_META)
        assert a2a.requests == [] and cid not in runtime._answering
        assert runtime.store.get_pending_confirmation(cid)["kind"] == "credential_consent"

        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)
        assert done.state == A2ATaskState.completed, done.text
        assert len(a2a.requests) == 1

    asyncio.run(go())


def test_a_grant_that_cant_be_recorded_does_not_undo_the_consented_call() -> None:
    """Grants are recorded after the call, so the call runs once on the consent just given;
    a failure to record it only means the owner is asked again next time."""
    class ReadOnlyGate(InMemoryCredentialGate):
        async def add_grant(self, user_id, agent_id, cred_type):
            raise OSError("credential store unreachable")

    async def go():
        gate = ReadOnlyGate()
        runtime, a2a = _cred_bot_runtime(gate)
        a2a.release.set()
        cid = await _ask_consent(runtime)

        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)

        assert done.state == A2ATaskState.completed, done.text
        assert len(a2a.requests) == 1
        assert a2a.requests[0].metadata["credentials"] == OWNER_META["credentials"]
        assert runtime.store.get_pending_confirmation(cid) is None
        assert await gate.list_granted("owner-id", "cred-bot") == set()

    asyncio.run(go())


def test_a_newer_prompt_saved_meanwhile_is_left_for_its_own_answer() -> None:
    """An answer read one prompt; if a newer prompt for the conversation replaced it before
    the claim, the answer runs neither, and the newer one stays pending."""
    newer = {"kind": "credential_consent", "skill": "credbot/other", "grant_types": ["openai_api_key"]}

    class ReplacingGate(InMemoryCredentialGate):
        runtime = None
        cid = None

        async def list_granted(self, user_id, agent_id):
            if self.runtime is not None:  # a concurrent turn raises a new prompt here
                await self.runtime._save_pending(self.cid, OWNER, dict(newer))
                self.runtime = None
            return await super().list_granted(user_id, agent_id)

    async def go():
        gate = ReplacingGate()
        runtime, a2a = _cred_bot_runtime(gate)
        cid = await _ask_consent(runtime)
        gate.runtime, gate.cid = runtime, cid

        _refused(await _say(runtime, OWNER, "yes", cid, OWNER_META), "no_pending")

        assert a2a.requests == []
        assert runtime.store.get_pending_confirmation(cid) == newer

    asyncio.run(go())
