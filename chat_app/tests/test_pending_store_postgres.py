"""pending_store's statements on a real Postgres.

The other confirmation tests use a stand-in that follows these statements; this checks the
statements themselves: the claim-column migration, a claim matching the payload a JSON
round trip gives back, refresh/release/finish acting only on their own claim, the claim
expiry, one winner among concurrent claims, a claim applied after its release (in either
order behind a lock), and a refresh ending on a dead connection.

Skipped unless PLANNER_TEST_DATABASE_DSN names a scratch database (each test drops and
recreates pending_confirmations there), for example a throwaway container from the repo's
Postgres image; see docs/TESTING.md.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

import asyncpg
import pytest

from chat_app.store import conversation_store, db, pending_store
from chat_app.planner.models import A2ATaskState, Channel

from chat_app.tests.test_confirmation_claim import SlowA2A, _answer, _refused
from chat_app.tests.test_confirmation_ownership import OWNER, OWNER_META, _say, _write_runtime

DSN = os.getenv("PLANNER_TEST_DATABASE_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="needs PLANNER_TEST_DATABASE_DSN (a scratch Postgres)")

OLD_TABLE = """
CREATE TABLE pending_confirmations (
    context_id   TEXT        PRIMARY KEY,
    owner_email  TEXT        NOT NULL,
    payload      JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
PROMPT = {
    "skill": "email/send",
    "task_input": {"to": ["bø@example.test"], "subject": "Ünïcode ✓", "body": "b", "n": 3, "x": 0.1},
    "selected_agent": {"agent_id": "email-agent", "tags": [], "meta": None, "ok": True},
    "prompt_id": "a" * 32,
}


def _run(body):
    """Run ``body`` against a fresh pending_confirmations table, in the pre-claim schema
    the migration has to upgrade."""
    async def go():
        db._pool = None
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute("DROP TABLE IF EXISTS pending_confirmations")
            await conn.execute(OLD_TABLE)
        finally:
            await conn.close()
        await db.init_db()
        try:
            await body()
        finally:
            await db._pool.close()
            db._pool = None

    asyncio.run(go())


@pytest.fixture(autouse=True)
def _database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN or "")


async def _sql(query, *args):
    async with (await db.get_pool()).acquire() as conn:
        return await conn.fetchval(query, *args)


def test_the_migration_adds_the_claim_columns_and_keeps_existing_rows() -> None:
    async def body():
        await _sql("DROP TABLE pending_confirmations")
        await _sql(OLD_TABLE)
        await _sql("INSERT INTO pending_confirmations VALUES ('c1', $1, '{\"skill\": \"x\"}')", OWNER)

        for _ in range(2):  # every startup runs it
            async with (await db.get_pool()).acquire() as conn:
                await conn.execute(db._SCHEMA_SQL)

        assert await pending_store.get("c1") == ({"skill": "x"}, False)
        assert await pending_store.claim("c1", {"skill": "x"}, "answer-1")

    _run(body)


def test_an_answer_claims_only_the_unclaimed_prompt_it_read_and_finish_deletes_it() -> None:
    async def body():
        await pending_store.save("c1", OWNER, PROMPT)
        read, claimed = await pending_store.get("c1")
        assert read == PROMPT and claimed is False

        assert not await pending_store.claim("c1", {**read, "prompt_id": "b" * 32}, "answer-1")
        assert not await pending_store.claim("c2", read, "answer-1")
        assert await pending_store.claim("c1", dict(reversed(list(read.items()))), "answer-1")  # key order is irrelevant
        assert await pending_store.get("c1") == (PROMPT, True)
        assert not await pending_store.claim("c1", read, "answer-2")
        assert await pending_store.owner("c1") == OWNER

        await pending_store.finish("c1", "answer-2")
        assert await pending_store.get("c1") == (PROMPT, True)
        await pending_store.finish("c1", "answer-1")
        assert await pending_store.get("c1") is None

    _run(body)


def test_a_new_prompt_replaces_a_claimed_row_and_outlives_its_finish() -> None:
    async def body():
        newer = {**PROMPT, "prompt_id": "b" * 32}
        await pending_store.save("c1", OWNER, PROMPT)
        assert await pending_store.claim("c1", PROMPT, "answer-1")

        await pending_store.release("c1", "answer-0")
        await pending_store.save("c1", OWNER, newer)
        assert await _sql("SELECT released_claim_ids FROM pending_confirmations") is None  # a fresh list
        for cleanup in (pending_store.refresh, pending_store.finish, pending_store.release):
            await cleanup("c1", "answer-1")  # the old answer's: none touches the new prompt
            assert await pending_store.get("c1") == (newer, False)
        assert await pending_store.claim("c1", newer, "answer-2")

    _run(body)


def test_an_expired_claim_reads_as_nothing_and_is_never_claimed_again() -> None:
    async def body():
        await pending_store.save("c1", OWNER, PROMPT)
        assert await pending_store.claim("c1", PROMPT, "answer-1")
        await _sql(
            "UPDATE pending_confirmations SET claimed_at = now() - make_interval(secs => $1)",
            pending_store.CLAIM_TTL_SECONDS - 5,
        )
        assert await pending_store.get("c1") == (PROMPT, True)

        await _sql(
            "UPDATE pending_confirmations SET claimed_at = now() - make_interval(secs => $1)",
            pending_store.CLAIM_TTL_SECONDS + 5,
        )
        assert await pending_store.get("c1") is None
        assert not await pending_store.claim("c1", PROMPT, "answer-2")
        assert await pending_store.owner("c1") == OWNER  # still guards the conversation

        await pending_store.save("c1", OWNER, PROMPT)  # the next prompt starts unclaimed
        assert await pending_store.get("c1") == (PROMPT, False)

    _run(body)


def test_a_refresh_keeps_only_the_claim_it_names_fresh() -> None:
    async def body():
        await pending_store.save("c1", OWNER, PROMPT)
        await pending_store.refresh("c1", "answer-1")  # nothing claimed: stays unclaimed
        assert await pending_store.get("c1") == (PROMPT, False)

        assert await pending_store.claim("c1", PROMPT, "answer-1")
        await _sql(
            "UPDATE pending_confirmations SET claimed_at = now() - make_interval(secs => $1)",
            pending_store.CLAIM_TTL_SECONDS + 5,
        )
        await pending_store.refresh("c1", "answer-2")  # another answer: no effect
        assert await pending_store.get("c1") is None
        await pending_store.refresh("c1", "answer-1")  # its own answer is still acting on it
        assert await pending_store.get("c1") == (PROMPT, True)
        assert not await pending_store.claim("c1", PROMPT, "answer-2")

    _run(body)


def test_a_release_gives_back_only_its_own_claim() -> None:
    async def body():
        await pending_store.save("c1", OWNER, PROMPT)
        assert await pending_store.claim("c1", PROMPT, "answer-1")

        await pending_store.release("c1", "answer-2")
        assert await pending_store.get("c1") == (PROMPT, True)
        await pending_store.release("c1", "answer-1")
        assert await pending_store.get("c1") == (PROMPT, False)
        await pending_store.finish("c1", "answer-1")  # no longer its claim
        assert await pending_store.get("c1") == (PROMPT, False)
        assert await pending_store.claim("c1", PROMPT, "answer-3")

    _run(body)


class _StallingProxy:
    """A TCP proxy to the test database that can stop forwarding on the connections it has
    open, like a connection whose peer silently went away."""

    def __init__(self) -> None:
        self.stalled = False
        self._open: list[asyncio.StreamWriter] = []

    async def __aenter__(self) -> str:
        target = urlsplit(DSN)
        self._target = (target.hostname, target.port or 5432)
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return target._replace(netloc=f"{target.username}@127.0.0.1:{port}").geturl()

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        for writer in self._open:
            writer.close()

    async def _serve(self, client_reader, client_writer) -> None:
        stalls = not self.stalled  # connections opened while stalled (a reconnect) work
        server_reader, server_writer = await asyncio.open_connection(*self._target)
        self._open += [client_writer, server_writer]

        async def pipe(reader, writer):
            while data := await reader.read(65536):
                if not (stalls and self.stalled):
                    writer.write(data)

        await asyncio.gather(pipe(client_reader, server_writer), pipe(server_reader, client_writer),
                             return_exceptions=True)


async def _refresh_attempt() -> asyncio.Future:
    """Start a refresh of c1's claim, called directly (the runtime adds its own bound), and
    return it once it has ended or had 1.5 times its bound to end."""
    attempt = asyncio.ensure_future(pending_store.refresh("c1", "answer-1"))
    await asyncio.wait({attempt}, timeout=1.5 * pending_store.CLAIM_REFRESH_TIMEOUT_SECONDS)
    return attempt


def _with_a_stalling_pool(check, **pool_options) -> None:
    """Run ``check(proxy)`` with pending_store on a one-connection pool that reaches the
    database through a _StallingProxy; ``pool_options`` map pool arguments to factories
    taking the proxy."""
    async def body():
        await pending_store.save("c1", OWNER, PROMPT)
        assert await pending_store.claim("c1", PROMPT, "answer-1")
        direct, proxy = db._pool, _StallingProxy()
        async with proxy as proxied_dsn:
            db._pool = await asyncpg.create_pool(
                proxied_dsn, min_size=1, max_size=1, **{k: v(proxy) for k, v in pool_options.items()},
            )
            try:
                await check(proxy)
                await pending_store.refresh("c1", "answer-1")  # the pool recovers on a new connection
            finally:
                db._pool.terminate()
                db._pool = direct
        assert await pending_store.get("c1") == (PROMPT, True)

    _run(body)


def test_a_refresh_on_a_connection_that_stops_answering_still_ends(monkeypatch) -> None:
    """After a statement is cancelled, asyncpg waits for the server to confirm the cancel
    before reusing the connection; a dead connection never does. The refresh drops the
    connection instead, so a hung attempt ends at its bound and the next one can keep the
    claim fresh."""
    monkeypatch.setattr(pending_store, "CLAIM_REFRESH_TIMEOUT_SECONDS", 1.0)

    async def check(proxy):
        proxy.stalled = True
        attempt = await _refresh_attempt()
        assert attempt.done()  # given back, the connection would wait for a reply
        assert isinstance(attempt.exception(), Exception)

    _with_a_stalling_pool(check)


def test_a_refresh_whose_connection_stops_answering_once_it_is_done_still_ends(monkeypatch) -> None:
    """A connection is reset as it is given back. If it stops answering right after the
    refresh statement, the refresh's bound covers that reset too."""
    monkeypatch.setattr(pending_store, "CLAIM_REFRESH_TIMEOUT_SECONDS", 1.0)

    def reset_stalls(proxy):
        async def reset(conn):
            proxy.stalled = True
            await conn.execute("SELECT 1")
        return reset

    async def check(proxy):
        attempt = await _refresh_attempt()
        assert attempt.done()  # without a bound the reset waits forever
        assert isinstance(attempt.exception(), Exception)

    _with_a_stalling_pool(check, reset=reset_stalls)


async def _until_waiting_on_a_lock(watch, count, unless=None) -> None:
    for _ in range(200):
        if unless is not None and unless.done():
            return
        waiting = await watch.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND datname = current_database()"
        )
        if waiting == count:
            return
        await asyncio.sleep(0.01)


def test_a_claim_that_reaches_the_row_after_its_release_takes_nothing() -> None:
    """A claim whose connection dropped can still reach the row after its release, here
    once someone else's lock on the row is released. Every given-back id is recorded, so
    even after another answer's release that claim takes nothing."""
    async def body():
        await pending_store.save("c1", OWNER, PROMPT)
        for given_back in ("answer-1", "answer-2"):
            await pending_store.release("c1", given_back)
        for late in ("answer-1", "answer-2"):
            assert not await pending_store.claim("c1", PROMPT, late)
        assert await pending_store.get("c1") == (PROMPT, False)

        holder, watch = await asyncpg.connect(DSN), await asyncpg.connect(DSN)
        try:
            locked = holder.transaction()
            await locked.start()
            await holder.execute("SELECT 1 FROM pending_confirmations WHERE context_id = 'c1' FOR UPDATE")
            releasing = asyncio.ensure_future(pending_store.release("c1", "answer-3"))
            await _until_waiting_on_a_lock(watch, 1)
            claiming = asyncio.ensure_future(pending_store.claim("c1", PROMPT, "answer-3"))  # queued after
            await _until_waiting_on_a_lock(watch, 2)
            await locked.commit()
            await asyncio.wait_for(releasing, 2)
            assert await asyncio.wait_for(claiming, 2) is False
        finally:
            await holder.close()
            await watch.close()
        assert await pending_store.get("c1") == (PROMPT, False)
        assert await pending_store.claim("c1", PROMPT, "answer-4")

    _run(body)


def test_a_claim_queued_on_the_row_before_its_release_is_given_back() -> None:
    """A claim whose connection dropped while it waited on someone else's lock on the row
    is applied once that lock is released. Its release, sent after the drop, waits on the
    same row and then gives it back, so the prompt ends up unclaimed."""
    async def body():
        await pending_store.save("c1", OWNER, PROMPT)
        holder, watch = await asyncpg.connect(DSN), await asyncpg.connect(DSN)
        try:
            locked = holder.transaction()
            await locked.start()
            await holder.execute("SELECT 1 FROM pending_confirmations WHERE context_id = 'c1' FOR UPDATE")
            direct = db._pool
            db._pool = await asyncpg.create_pool(DSN, min_size=1, max_size=1)
            claiming = asyncio.ensure_future(pending_store.claim("c1", PROMPT, "answer-1"))
            await _until_waiting_on_a_lock(watch, 1)
            db._pool.terminate()  # the claim's connection drops; its statement stays queued
            db._pool = direct
            with pytest.raises(Exception):
                await claiming

            releasing = asyncio.ensure_future(pending_store.release("c1", "answer-1"))
            await _until_waiting_on_a_lock(watch, 2, unless=releasing)
            await locked.commit()
            await asyncio.wait_for(releasing, 2)
        finally:
            await holder.close()
            await watch.close()

        assert await pending_store.get("c1") == (PROMPT, False)
        assert await pending_store.claim("c1", PROMPT, "answer-2")

    _run(body)


def test_of_eight_concurrent_claims_one_wins() -> None:
    async def body():
        await pending_store.save("c1", OWNER, PROMPT)
        won = await asyncio.gather(*(pending_store.claim("c1", PROMPT, f"answer-{n}") for n in range(8)))
        assert sorted(won) == [False] * 7 + [True]

    _run(body)


def test_a_late_yes_on_another_process_is_told_the_claimed_call_is_in_progress() -> None:
    """The runtime on the real statements: process 1's confirmed call is held while
    process 2 answers."""
    async def body():
        first_process, _ = _write_runtime()
        first_process.a2a_client = slow = SlowA2A()
        cid = (await _say(first_process, OWNER, "Send the email.", metadata=OWNER_META)).context_id
        await conversation_store.save(cid, first_process.store.get_context(cid).conversation_history, OWNER)
        second_process, second_a2a = _write_runtime()
        await second_process._owned_context(OWNER, Channel.api, cid)

        running = asyncio.create_task(_say(first_process, OWNER, "yes", cid, OWNER_META))
        await asyncio.wait_for(slow.entered.wait(), 2)
        _refused(await _say(second_process, OWNER, "yes", cid, OWNER_META), "answer_in_progress")
        _refused(await _answer(second_process, cid, confirmed=False), "answer_in_progress")
        slow.release.set()

        assert (await running).state == A2ATaskState.completed
        assert slow.calls == [("email-agent", "email/send")] and second_a2a.calls == []
        assert await pending_store.get(cid) is None
        _refused(await _answer(second_process, cid), "no_pending")

    _run(body)
