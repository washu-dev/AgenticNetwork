"""Postgres-backed durable store for pending confirmations (P4g/H2b).

A write-skill or credential-consent prompt is saved keyed by context_id and read
back on the user's next turn. In-memory that state is process-local, so a "yes"
landing on a different chat app process (or after a restart) would find nothing.
This module persists it to Postgres (table `pending_confirmations`, created in
`db._SCHEMA_SQL`) so the resume works across processes.

The runtime keeps an in-memory cache, but when a DB is configured the row is the
source of truth and is read on every lookup: another process may have resumed or
cancelled the action since this one cached it. An answer (yes, no, the confirm
button) acts only on a row it claimed itself with `claim`, which marks the row
(`claimed_at`, and the answer's own `claim_id`) only if it is still unclaimed and
still the prompt the answer read, so two answers arriving together cannot both run
the action and an answer never runs a newer prompt it didn't see. The row stays
while the agent call runs (the answer keeps its claim fresh with `refresh`), so a
late answer on any process is told the action is in progress, and `finish` deletes
it afterwards; `release` gives it back when the answer ended before acting. Those
three match the claim_id, so they never touch another answer's claim or a newer
prompt. A claim not refreshed for CLAIM_TTL_SECONDS (its process died mid-call)
reads as no row and can never be claimed again: the action runs at most once.
Lookups fall back to the cache only when the DB is unreachable; a claim never does
(the runtime refuses the answer instead). When no DB is configured (Tier A/B laptop
runs, unit tests) every function is a no-op and the in-memory cache is the only store.
"""
from __future__ import annotations

import json
from typing import Any

from chat_app.store.db import configured, get_pool

# A claimed row reads as "in progress" until CLAIM_TTL_SECONDS after its claim was last
# refreshed, then as gone. The answer refreshes it every CLAIM_REFRESH_SECONDS while its
# agent call runs (which can take up to an hour), each attempt ending within
# CLAIM_REFRESH_TIMEOUT_SECONDS, so a live claim expires only when three attempts in a row
# fail. Otherwise a claim expires only when its process died or its delete or release
# failed.
CLAIM_TTL_SECONDS = 120.0
CLAIM_REFRESH_SECONDS = 20.0
CLAIM_REFRESH_TIMEOUT_SECONDS = 10.0


def enabled() -> bool:
    """Whether pending confirmations are persisted (a shared DB is configured)."""
    return configured()


async def save(context_id: str, owner_email: str, payload: dict[str, Any]) -> None:
    """Persist (or replace, claimed or not) the pending confirmation for a context_id."""
    if not enabled():
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pending_confirmations (context_id, owner_email, payload) "
            "VALUES ($1, $2, $3::jsonb) "
            "ON CONFLICT (context_id) DO UPDATE SET "
            "owner_email = EXCLUDED.owner_email, payload = EXCLUDED.payload, created_at = now(), "
            "claimed_at = NULL, claim_id = NULL, released_claim_ids = NULL",
            context_id,
            owner_email,
            json.dumps(payload),
        )


async def get(context_id: str) -> tuple[dict[str, Any], bool] | None:
    """Return (payload, claimed) for a context_id's pending confirmation, or None.

    claimed is True while an answer is acting on it. A claim not refreshed for
    CLAIM_TTL_SECONDS reads as None.
    """
    if not enabled():
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload, claimed_at IS NOT NULL AS claimed FROM pending_confirmations "
            "WHERE context_id = $1 "
            "AND (claimed_at IS NULL OR claimed_at > now() - make_interval(secs => $2))",
            context_id,
            CLAIM_TTL_SECONDS,
        )
    return (_payload(row), row["claimed"]) if row else None


async def claim(context_id: str, payload: dict[str, Any], claim_id: str) -> bool:
    """Claim the pending confirmation for the answer ``claim_id`` if it is unclaimed and
    still ``payload``.

    One statement, so when two answers race (a double click, a client retry, replies on
    two processes) exactly one of them claims it. Matching the whole payload means an
    answer can't claim a newer prompt that replaced the one it read. A claim id already
    given back (``release``) claims nothing.
    """
    if not enabled():
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            "UPDATE pending_confirmations SET claimed_at = now(), claim_id = $3 "
            "WHERE context_id = $1 AND claimed_at IS NULL AND payload = $2::jsonb "
            "AND $3 <> ALL(COALESCE(released_claim_ids, '{}')) "
            "RETURNING true",
            context_id,
            json.dumps(payload),
            claim_id,
        )
    return bool(claimed)


async def refresh(context_id: str, claim_id: str) -> None:
    """Restart the expiry of the claim ``claim_id`` while its answer is still acting.

    Bounded by CLAIM_REFRESH_TIMEOUT_SECONDS (the caller also bounds the whole attempt)."""
    await _execute(
        "UPDATE pending_confirmations SET claimed_at = now() WHERE context_id = $1 AND claim_id = $2",
        context_id, claim_id, timeout=CLAIM_REFRESH_TIMEOUT_SECONDS,
    )


async def release(context_id: str, claim_id: str) -> None:
    """Give the claim ``claim_id`` back unused, so a later answer can act on the prompt.

    The claim statement may not have been applied yet: its connection dropped and the
    database still runs it. The release matches the row by context_id, so it takes the
    row lock whether or not the claim is there: a claim still queued on the row (behind
    someone else's lock) is applied first and then given back here. And the release
    records the id as given back, so a claim statement that reaches the row only after
    the release (for example when waiters on a table lock race) takes nothing."""
    await _execute(
        "UPDATE pending_confirmations SET "
        "claimed_at = CASE WHEN claim_id = $2 THEN NULL ELSE claimed_at END, "
        "claim_id = CASE WHEN claim_id = $2 THEN NULL ELSE claim_id END, "
        "released_claim_ids = array_append(COALESCE(released_claim_ids, '{}'), $2) "
        "WHERE context_id = $1",
        context_id, claim_id,
    )


async def finish(context_id: str, claim_id: str) -> None:
    """Delete the row the answer ``claim_id`` claimed, once it is done, unless a newer
    prompt replaced it (saving one clears the claim)."""
    await _execute(
        "DELETE FROM pending_confirmations WHERE context_id = $1 AND claim_id = $2",
        context_id, claim_id,
    )


async def _execute(query: str, *args: Any, timeout: float | None = None) -> None:
    if not enabled():
        return
    pool = await get_pool()
    async with pool.acquire(timeout=timeout) as conn:
        try:
            await conn.execute(query, *args, timeout=timeout)
        except BaseException:
            # Drop the connection rather than give it back. After a failed or cancelled
            # statement asyncpg waits for the server to confirm the cancel before reusing
            # the connection, which a dead connection never does, and a wait cut short by
            # a timeout can close the next user's connection.
            conn.terminate()
            raise


def _payload(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = row["payload"]
    # asyncpg returns JSONB as a str unless a codec is registered; decode defensively.
    return json.loads(payload) if isinstance(payload, str) else payload


async def owner(context_id: str) -> str | None:
    """Return the owner_email of a persisted pending confirmation, or None."""
    if not enabled():
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT owner_email FROM pending_confirmations WHERE context_id = $1",
            context_id,
        )
    return row["owner_email"] if row else None


async def clear(context_id: str) -> None:
    """Remove any persisted pending confirmation for a context_id."""
    if not enabled():
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM pending_confirmations WHERE context_id = $1", context_id)
