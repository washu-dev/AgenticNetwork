"""One answer per pending action, and only to the prompt it was shown on.

Two answers to the same pending write confirmation or credential consent can arrive
together: a double click on the confirm button, a client retry, a chat "yes" racing the
button, or replies landing on two planner processes. Each answer used to read the action,
send the agent call and clear the action only afterwards, so both ran the write (the same
email sent twice). Now (PlannerRuntime._answer_pending):

- within one process, one answer per conversation runs at a time; another gets
  ``answer_in_progress`` and leaves the action alone;
- an answer does what can fail first, then claims the action it read right before the
  agent call (with a database one UPDATE marks the row claimed if it is unclaimed and
  unchanged; without one the in-memory copy is removed), so across processes exactly one
  answer acts and the other gets ``no_pending``;
- with a database the claimed row stays until the answer is done, so an answer arriving
  on any process meanwhile gets ``answer_in_progress``; a claim that is never finished
  (its process died) expires and is never claimed again;
- the confirm button sends the prompt_id it was shown with, and a press on a prompt that
  a newer one replaced gets ``superseded``;
- a claim the database can't serve runs nothing (``pending_store_unavailable``).

The races hold the first answer inside its agent call (SlowA2A) while the second arrives,
which is when both used to run.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from uuid import uuid4

import httpx
import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentSkill

from chat_app.store import pending_store
from chat_app.planner.credential_gate import InMemoryCredentialGate
from agentnet.directory.card_oasf import (
    REQUIRED_CREDENTIALS_EXTENSION_URI,
    load_agent_card,
)
from chat_app.planner.llm_client import ScriptedLLMClient
from chat_app.planner.models import A2ATaskState, Channel
from chat_app.planner.runtime import PlannerRuntime
from chat_app.server import create_app
from chat_app.planner.memory_store import MemoryStore

from chat_app.tests.test_confirmation_ownership import (  # noqa: F401 (persisted is a fixture)
    CALL_CRED_BOT, OWNER, OWNER_META, SEND_EMAIL, _say, _write_runtime, persisted,
)
from chat_app.tests.test_runtime import EMAIL_CARD, SpyA2A, SpyDirectory, _config


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


def _email_app(script=SEND_EMAIL) -> tuple[object, SlowA2A]:
    """A planner whose email/send waits for a yes (strict policy)."""
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SlowA2A()
    app = create_app(
        config=_config("strict"),
        directory_client=directory,
        a2a_client=a2a,
        state_store=MemoryStore(),
        llm_client=ScriptedLLMClient(list(script)),
    )
    return app, a2a


def _cred_bot_runtime(gate, script=CALL_CRED_BOT) -> tuple[PlannerRuntime, SlowA2A]:
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
    runtime = PlannerRuntime(
        config=_config(), directory_client=directory, a2a_client=a2a,
        state_store=MemoryStore(), llm_client=ScriptedLLMClient(list(script)),
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
    pending = runtime.store.get_pending_confirmation(cid)
    assert pending["kind"] == "credential_consent" and re.fullmatch("[0-9a-f]{32}", pending["prompt_id"])
    return cid


async def _press(app, context_id, confirmed, prompt_id=None):
    body = {"context_id": context_id, "confirmed": confirmed}
    if prompt_id is not None:
        body["prompt_id"] = prompt_id
    return await _post(app, "/confirmations", body)


def _answer(runtime, context_id, confirmed=True, prompt_id=None):
    return runtime._answer_pending(
        context_id, Channel.api, dict(OWNER_META), confirmed=confirmed, prompt_id=prompt_id,
    )


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
    """With a database the claim is a round trip: the row is claimed before the first
    answer resumes. A repeat in that gap must still not start an LLM turn."""
    conversations, pending_rows = persisted
    claiming = pending_store.claim
    claimed = asyncio.Event()

    async def slow_claim(context_id, payload, claim_id):
        won = await claiming(context_id, payload, claim_id)
        claimed.set()
        await asyncio.sleep(0.05)  # the reply is still on its way back
        return won

    monkeypatch.setattr(pending_store, "claim", slow_claim)

    async def go():
        gate = InMemoryCredentialGate()
        runtime, a2a = _cred_bot_runtime(gate, [*CALL_CRED_BOT, *CALL_CRED_BOT])
        a2a.release.set()
        cid = await _ask_consent(runtime)

        first = asyncio.create_task(_say(runtime, OWNER, "yes", cid, OWNER_META))
        await asyncio.wait_for(claimed.wait(), 2)
        assert cid in pending_rows.claimed
        second = await _say(runtime, OWNER, "yes", cid, OWNER_META)

        _refused(second, "answer_in_progress")
        assert (await first).state == A2ATaskState.completed
        assert len(a2a.requests) == 1

    asyncio.run(go())


# ── Answers on two processes (a shared database stand-in) ─────────────────────


def test_answers_on_two_processes_at_once_run_it_once(persisted, monkeypatch) -> None:
    """Both processes read the pending row before either claims it; the claim decides. The
    loser leaves the winner's claim alone, so an answer arriving after it is told the action
    is in progress."""
    conversations, pending_rows = persisted
    claiming = pending_store.claim

    async def yielding_claim(context_id, payload, claim_id):
        await asyncio.sleep(0)  # let the other process read the row first
        return await claiming(context_id, payload, claim_id)

    monkeypatch.setattr(pending_store, "claim", yielding_claim)

    async def go():
        first_process, _ = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        conversations[cid] = (OWNER, list(first_process.store.get_context(cid).conversation_history))
        second_process, _ = _write_runtime()
        await second_process._owned_context(OWNER, Channel.api, cid)  # rehydrates the conversation
        first_process.a2a_client = second_process.a2a_client = slow = SlowA2A()

        answers = {asyncio.create_task(_answer(rt, cid)): rt for rt in (first_process, second_process)}
        await asyncio.wait_for(slow.entered.wait(), 2)
        [lost], [won] = await asyncio.wait(answers, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)

        _refused(lost.result(), "no_pending")
        assert cid in pending_rows.claimed
        _refused(await _say(answers[lost], OWNER, "yes", cid, OWNER_META), "answer_in_progress")
        slow.release.set()
        assert (await won).state == A2ATaskState.completed
        assert slow.calls == [("email-agent", "email/send")]
        assert cid not in pending_rows

    asyncio.run(go())


def test_closing_the_stream_mid_call_still_deletes_the_claimed_row(persisted, monkeypatch) -> None:
    """Closing the chat stream cancels the answer inside its agent call, and the server keeps
    cancelling it while it waits (anyio's cancel scope). The row's delete must still run, or
    the claim would turn away every answer to the prompt until it expired."""
    conversations, pending_rows = persisted
    deleting = pending_store.finish
    started, go_on = asyncio.Event(), asyncio.Event()

    async def held_finish(context_id, claim_id):
        started.set()
        await go_on.wait()  # the DELETE is on its way
        await deleting(context_id, claim_id)

    monkeypatch.setattr(pending_store, "finish", held_finish)

    async def go():
        runtime, _ = _write_runtime()
        runtime.a2a_client = slow = SlowA2A()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        answering = asyncio.create_task(_say(runtime, OWNER, "yes", cid, OWNER_META))
        await asyncio.wait_for(slow.entered.wait(), 2)

        answering.cancel()  # the stream closes during the call
        await asyncio.wait_for(started.wait(), 2)
        answering.cancel()  # and the answer is cancelled again while the delete runs
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(answering, 2)

        assert cid in pending_rows.claimed and cid not in runtime._answering
        go_on.set()
        await asyncio.wait_for(asyncio.gather(*runtime._cleanups), 2)
        assert cid not in pending_rows and not runtime._cleanups
        assert slow.calls == []

    asyncio.run(go())


def test_an_answer_cancelled_during_its_claim_gives_the_claim_back(persisted, monkeypatch) -> None:
    """The claim's UPDATE can be applied by the database while the answer is cancelled (the
    stream closes) before its reply arrives. Nothing will act on that claim, so it is
    released, and the next answer runs the action."""
    conversations, pending_rows = persisted
    claiming = pending_store.claim
    applied, reply = asyncio.Event(), asyncio.Event()

    async def held_claim(context_id, payload, claim_id):
        won = await claiming(context_id, payload, claim_id)
        applied.set()
        await reply.wait()  # the reply is still on its way back
        return won

    monkeypatch.setattr(pending_store, "claim", held_claim)

    async def go():
        runtime, a2a = _write_runtime()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        answering = asyncio.create_task(_say(runtime, OWNER, "yes", cid, OWNER_META))
        await asyncio.wait_for(applied.wait(), 2)

        answering.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(answering, 2)
        assert cid in pending_rows.claimed
        reply.set()
        await asyncio.wait_for(asyncio.gather(*runtime._cleanups), 2)

        assert pending_rows.waiting(cid) and a2a.calls == []
        monkeypatch.setattr(pending_store, "claim", claiming)
        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)
        assert done.state == A2ATaskState.completed, done.text
        assert a2a.calls == [("email-agent", "email/send")] and cid not in pending_rows

    asyncio.run(go())


def test_a_claim_whose_reply_is_lost_is_given_back(persisted, monkeypatch) -> None:
    """The database applied the claim but its reply never arrived (the connection dropped).
    The answer is refused, so the claim is released for the owner's next answer."""
    conversations, pending_rows = persisted
    claiming = pending_store.claim

    async def reply_lost(context_id, payload, claim_id):
        await claiming(context_id, payload, claim_id)
        raise ConnectionResetError("connection reset")

    monkeypatch.setattr(pending_store, "claim", reply_lost)

    async def go():
        runtime, a2a = _write_runtime()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id

        _refused(await _say(runtime, OWNER, "yes", cid, OWNER_META), "pending_store_unavailable")
        await asyncio.wait_for(asyncio.gather(*runtime._cleanups), 2)

        assert pending_rows.waiting(cid) and a2a.calls == []
        monkeypatch.setattr(pending_store, "claim", claiming)
        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)
        assert done.state == A2ATaskState.completed, done.text

    asyncio.run(go())


def test_a_claim_applied_only_after_its_release_takes_nothing(persisted, monkeypatch) -> None:
    """The claim statement can reach the row after the answer gave up on it and released it
    (its connection dropped, and the statement was held up, for example behind a table
    lock). The release recorded its id, so it takes nothing and the owner's next answer runs
    the action."""
    conversations, pending_rows = persisted
    claiming = pending_store.claim
    sent: list[tuple] = []

    async def not_applied_yet(context_id, payload, claim_id):
        sent.append((context_id, payload, claim_id))
        raise ConnectionResetError("connection reset")

    monkeypatch.setattr(pending_store, "claim", not_applied_yet)

    async def go():
        runtime, a2a = _write_runtime()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        _refused(await _say(runtime, OWNER, "yes", cid, OWNER_META), "pending_store_unavailable")
        await asyncio.wait_for(asyncio.gather(*runtime._cleanups), 2)

        assert not await claiming(*sent[0])  # the database applies it now
        assert pending_rows.waiting(cid) and a2a.calls == []
        monkeypatch.setattr(pending_store, "claim", claiming)
        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)
        assert done.state == A2ATaskState.completed, done.text

    asyncio.run(go())


def test_a_claim_stays_in_progress_for_as_long_as_its_call_runs(persisted, monkeypatch) -> None:
    """An agent call can outlast the claim's expiry (registered agents run up to an hour), so
    the answering process keeps refreshing the claim, each attempt bounded so a hung one
    doesn't stop the rest: a late answer on another process is told it's in progress however
    long the call takes. The refreshing stops with the answer."""
    conversations, pending_rows = persisted
    monkeypatch.setattr(pending_store, "CLAIM_REFRESH_SECONDS", 0.01)
    monkeypatch.setattr(pending_store, "CLAIM_REFRESH_TIMEOUT_SECONDS", 0.01)
    refreshing = pending_store.refresh
    refreshes: list[str] = []
    refreshed = asyncio.Event()

    async def first_one_hangs(context_id, claim_id):
        refreshes.append(context_id)
        if len(refreshes) == 1:
            await asyncio.Event().wait()  # a connection that never answers
        await refreshing(context_id, claim_id)
        refreshed.set()

    monkeypatch.setattr(pending_store, "refresh", first_one_hangs)

    async def go():
        first_process, _ = _write_runtime()
        first_process.a2a_client = slow = SlowA2A()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        conversations[cid] = (OWNER, list(first_process.store.get_context(cid).conversation_history))
        second_process, second_a2a = _write_runtime()
        await second_process._owned_context(OWNER, Channel.api, cid)
        running = asyncio.create_task(_say(first_process, OWNER, "yes", cid, OWNER_META))
        await asyncio.wait_for(slow.entered.wait(), 2)

        pending_rows.age_claim(cid, pending_store.CLAIM_TTL_SECONDS + 1)  # the call runs long
        assert await second_process._get_pending(cid) is None
        refreshed.clear()
        await asyncio.wait_for(refreshed.wait(), 2)
        _refused(await _say(second_process, OWNER, "yes", cid, OWNER_META), "answer_in_progress")
        slow.release.set()

        assert (await asyncio.wait_for(running, 2)).state == A2ATaskState.completed
        assert slow.calls == [("email-agent", "email/send")] and second_a2a.calls == []
        assert cid not in pending_rows
        count = len(refreshes)
        await asyncio.sleep(0.05)
        assert count > 1 and len(refreshes) == count

    asyncio.run(go())


def test_the_refreshing_stops_even_if_a_cancelled_refresh_comes_back_as_an_error(persisted, monkeypatch) -> None:
    """asyncpg can turn the cancellation of an in-flight query into a connection error. The
    refresh loop must still end with its answer, or it would keep a claim alive forever."""
    conversations, pending_rows = persisted
    monkeypatch.setattr(pending_store, "CLAIM_REFRESH_SECONDS", 0.01)
    monkeypatch.setattr(pending_store, "CLAIM_REFRESH_TIMEOUT_SECONDS", 5)
    refreshes: list[str] = []
    in_flight = asyncio.Event()

    async def cancel_becomes_an_error(context_id, claim_id):
        refreshes.append(context_id)
        in_flight.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if len(refreshes) > 1:
                raise  # only the first turns its cancel into an error
            raise ConnectionResetError("cancel request failed") from None

    monkeypatch.setattr(pending_store, "refresh", cancel_becomes_an_error)

    async def go():
        runtime, _ = _write_runtime()
        runtime.a2a_client = slow = SlowA2A()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        running = asyncio.create_task(_say(runtime, OWNER, "yes", cid, OWNER_META))
        await asyncio.wait_for(in_flight.wait(), 2)

        slow.release.set()  # the call ends while a refresh is in flight
        assert (await asyncio.wait_for(running, 2)).state == A2ATaskState.completed
        await asyncio.sleep(0.05)

        assert refreshes == [cid] and cid not in pending_rows

    asyncio.run(go())


@pytest.mark.parametrize("late", ["chat yes", "chat no", "button"])
def test_an_answer_on_another_process_while_the_press_runs_is_told_it_is_in_progress(
    persisted, monkeypatch, late,
) -> None:
    """The claimed row stays until the call is done, so the other process can tell. When
    the claim deleted the row, a late "yes" there found nothing and started an LLM turn."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conversations, pending_rows = persisted

    async def go():
        first_process, first_a2a = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        conversations[cid] = (OWNER, list(first_process.store.get_context(cid).conversation_history))
        app, second_a2a = _email_app()  # another process, with the confirm button
        late_answer = {
            "chat yes": lambda: _say(first_process, OWNER, "yes", cid, OWNER_META),
            "chat no": lambda: _say(first_process, OWNER, "no", cid, OWNER_META),
            "button": lambda: _answer(first_process, cid),
        }[late]()

        pressed, answered = await _while_first_runs(second_a2a, _press(app, cid, True), late_answer)

        assert pressed.json()["state"] == "completed", pressed.text
        _refused(answered, "answer_in_progress")
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
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conversations, pending_rows = persisted

    async def go():
        first_process, first_a2a = _write_runtime()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        assert cid not in conversations  # the history save failed; only the row knows the owner
        app, second_a2a = _email_app()

        refused = await _press(app, cid, True)

        assert refused.status_code == 200 and refused.json()["errors"] == ["unknown_context"], refused.text
        assert second_a2a.calls == [] and pending_rows.waiting(cid)
        done = await _answer(first_process, cid)
        assert done.state == A2ATaskState.completed, done.text
        assert first_a2a.calls == [("email-agent", "email/send")] and cid not in pending_rows

    asyncio.run(go())


def test_a_claim_the_database_cant_serve_runs_nothing_and_keeps_the_action(persisted, monkeypatch) -> None:
    """Running this process's copy when the claim fails would leave the row unclaimed for
    a later "yes", here or on another process, to run the write again, or to run it after
    the owner was told it was cancelled. So nothing runs and the owner answers again."""
    conversations, pending_rows = persisted
    working_claim = pending_store.claim
    database = {"up": False}

    async def flaky_claim(context_id, payload, claim_id):
        if not database["up"]:
            raise OSError("connection reset")
        return await working_claim(context_id, payload, claim_id)

    monkeypatch.setattr(pending_store, "claim", flaky_claim)

    async def go():
        runtime, a2a = _write_runtime()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id

        for reply in ("yes", "no"):
            _refused(await _say(runtime, OWNER, reply, cid, OWNER_META), "pending_store_unavailable")
        assert a2a.calls == [] and pending_rows.waiting(cid)
        assert runtime.store.get_pending_confirmation(cid) is not None

        database["up"] = True
        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)
        assert done.state == A2ATaskState.completed, done.text
        await _say(runtime, OWNER, "ok", cid, OWNER_META)

        assert a2a.calls == [("email-agent", "email/send")] and cid not in pending_rows

    asyncio.run(go())


def test_with_a_database_a_cancel_deletes_the_row(persisted) -> None:
    conversations, pending_rows = persisted

    async def go():
        runtime, a2a = _write_runtime()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id

        cancelled = await _say(runtime, OWNER, "no", cid, OWNER_META)

        assert cancelled.state == A2ATaskState.canceled, cancelled.text
        assert cid not in pending_rows and a2a.calls == []

    asyncio.run(go())


def test_with_a_database_an_allowed_consent_keeps_its_claim_until_the_grant_is_recorded(persisted) -> None:
    conversations, pending_rows = persisted

    class WatchingGate(InMemoryCredentialGate):
        def __init__(self) -> None:
            super().__init__()
            self.cid = None
            self.claimed_while_recording: list[bool] = []

        async def add_grant(self, user_id, agent_id, cred_type):
            self.claimed_while_recording.append(self.cid in pending_rows.claimed)
            await super().add_grant(user_id, agent_id, cred_type)

    async def go():
        gate = WatchingGate()
        runtime, a2a = _cred_bot_runtime(gate)
        a2a.release.set()
        gate.cid = cid = await _ask_consent(runtime)

        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)

        assert done.state == A2ATaskState.completed, done.text
        assert gate.claimed_while_recording == [True] and cid not in pending_rows
        assert await gate.list_granted("owner-id", "cred-bot") == {"openai_api_key"}

    asyncio.run(go())


def test_a_new_prompt_raised_while_the_claimed_call_runs_outlives_it(persisted, monkeypatch) -> None:
    """A message on another process can raise a new prompt while the confirmed call runs;
    saving it replaces the claimed row, and the finished answer deletes only the row it
    claimed, so the new prompt stays for its own answer."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conversations, pending_rows = persisted

    async def go():
        app, a2a = _email_app()
        cid = await _ask_to_send(app)
        conversations[cid] = (OWNER, ["user: Send the email."])
        other_process, other_a2a = _write_runtime()

        pressed, raised = await _while_first_runs(
            a2a, _press(app, cid, True), _say(other_process, OWNER, "Send the email.", cid, OWNER_META),
        )

        assert pressed.json()["state"] == "completed", pressed.text
        assert raised.confirmation_required, raised.text
        assert pending_rows.waiting(cid) and pending_rows[cid][1] == raised.pending_confirmation
        done = await _answer(other_process, cid)
        assert done.state == A2ATaskState.completed, done.text
        assert a2a.calls == other_a2a.calls == [("email-agent", "email/send")]
        assert cid not in pending_rows

    asyncio.run(go())


def test_a_claim_that_is_never_finished_never_runs_again(persisted, monkeypatch) -> None:
    """If the row can't be deleted after the call (the database dropped, or the process
    died mid-call), the claim stays: answers meanwhile are told it's in progress, and once
    it expires it reads as nothing pending. It is never claimed again."""
    conversations, pending_rows = persisted

    async def unreachable(context_id, claim_id):
        raise OSError("connection reset")

    monkeypatch.setattr(pending_store, "finish", unreachable)

    async def go():
        runtime, a2a = _write_runtime()
        cid = (await _say(runtime, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        conversations[cid] = (OWNER, list(runtime.store.get_context(cid).conversation_history))

        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)

        assert done.state == A2ATaskState.completed, done.text
        assert cid in pending_rows.claimed

        other_process, other_a2a = _write_runtime()
        await other_process._owned_context(OWNER, Channel.api, cid)
        for answer in (_say(runtime, OWNER, "yes", cid, OWNER_META), _answer(other_process, cid)):
            _refused(await answer, "answer_in_progress")

        pending_rows.age_claim(cid, pending_store.CLAIM_TTL_SECONDS + 1)
        assert await runtime._get_pending(cid) is None
        _refused(await _answer(other_process, cid), "no_pending")
        _refused(await _answer(runtime, cid, confirmed=False), "no_pending")
        assert not await pending_store.claim(cid, pending_rows[cid][1], "a later answer")
        assert a2a.calls == [("email-agent", "email/send")] and other_a2a.calls == []

    asyncio.run(go())


def test_pending_store_without_a_database_is_a_no_op(monkeypatch) -> None:
    async def no_pool():
        raise AssertionError("no database is configured")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(pending_store, "get_pool", no_pool)

    async def go():
        await pending_store.save("c1", OWNER, {"skill": "email/send"})
        assert await pending_store.get("c1") is None
        assert await pending_store.claim("c1", {"skill": "email/send"}, "answer-1") is False
        for cleanup in (pending_store.refresh, pending_store.release, pending_store.finish):
            await cleanup("c1", "answer-1")

    asyncio.run(go())


def test_a_live_claim_survives_two_refreshes_in_a_row_that_hang() -> None:
    """A claim's time is stamped when its refresh statement starts, which can land up to its
    bound later. Two attempts that hang for their full bound may follow, and the third must
    still land before the claim expires: a live claim expires only when three attempts in a
    row fail."""
    wait, bound = pending_store.CLAIM_REFRESH_SECONDS, pending_store.CLAIM_REFRESH_TIMEOUT_SECONDS
    assert bound + (wait + bound) * 3 < pending_store.CLAIM_TTL_SECONDS


# ── Which prompt an answer acts on ────────────────────────────────────────────


def test_a_press_on_a_replaced_prompt_runs_nothing() -> None:
    """The owner asks twice; the first card's buttons must not run or cancel the second
    prompt, which the owner may not have read yet."""
    async def go():
        app, a2a = _email_app([*SEND_EMAIL, *SEND_EMAIL])
        a2a.release.set()
        first = await _post(app, "/messages", {"channel": "api", "text": "Send the email."})
        cid = first.json()["context_id"]
        second = await _post(app, "/messages", {"channel": "api", "text": "Send it to Bo instead.", "context_id": cid})
        old_id = first.json()["pending_confirmation"]["prompt_id"]
        new_id = second.json()["pending_confirmation"]["prompt_id"]
        assert re.fullmatch("[0-9a-f]{32}", old_id) and new_id != old_id

        for confirmed in (True, False):
            _refused(await _press(app, cid, confirmed, old_id), "superseded")
        assert a2a.calls == []

        done = await _press(app, cid, True, new_id)
        assert done.json()["state"] == "completed", done.text
        assert [r.user_goal for r in a2a.requests] == ["Send it to Bo instead."]

    asyncio.run(go())


def test_a_prompt_or_press_without_a_prompt_id_still_works(persisted, monkeypatch) -> None:
    """Older clients send no prompt_id, and a row an older planner saved has none."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conversations, pending_rows = persisted

    async def go():
        app, a2a = _email_app()
        a2a.release.set()
        cid = await _ask_to_send(app)
        del pending_rows[cid][1]["prompt_id"]  # as an older planner saved it

        _refused(await _press(app, cid, True, "0" * 32), "superseded")
        done = await _press(app, cid, True)

        assert done.json()["state"] == "completed", done.text
        assert a2a.calls == [("email-agent", "email/send")] and cid not in pending_rows

    asyncio.run(go())


# ── What else an answer acts on ───────────────────────────────────────────────


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


@pytest.mark.parametrize("database", [False, True])
def test_a_newer_prompt_saved_meanwhile_is_left_for_its_own_answer(database, request) -> None:
    """An answer read one prompt; if a newer prompt for the conversation replaced it before
    the claim, the answer runs neither, and the newer one stays pending for its own answer."""
    pending_rows = request.getfixturevalue("persisted")[1] if database else None

    class ReplacingGate(InMemoryCredentialGate):
        runtime = cid = newer = None

        async def list_granted(self, user_id, agent_id):
            if self.runtime is not None:  # a concurrent turn raises a new prompt here
                runtime, self.runtime = self.runtime, None
                await runtime._save_pending(self.cid, OWNER, dict(self.newer))
            return await super().list_granted(user_id, agent_id)

    async def go():
        gate = ReplacingGate()
        runtime, a2a = _cred_bot_runtime(gate)
        a2a.release.set()
        cid = await _ask_consent(runtime)
        read = runtime.store.get_pending_confirmation(cid)
        gate.runtime, gate.cid = runtime, cid
        gate.newer = newer = {**read, "prompt_id": uuid4().hex, "user_goal": "do it again"}

        _refused(await _say(runtime, OWNER, "yes", cid, OWNER_META), "no_pending")

        assert a2a.requests == []
        if database:
            assert pending_rows.waiting(cid) and pending_rows[cid][1] == newer
        else:
            assert runtime.store.get_pending_confirmation(cid) == newer
        done = await _say(runtime, OWNER, "yes", cid, OWNER_META)
        assert done.state == A2ATaskState.completed, done.text
        assert [r.user_goal for r in a2a.requests] == ["do it again"]

    asyncio.run(go())
