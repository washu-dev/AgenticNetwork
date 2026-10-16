"""Postgres-backed durable store for pending confirmations (P4g/H2b).

A write-skill or credential-consent prompt is saved keyed by context_id and read
back on the user's next turn. In-memory that state is process-local, so a "yes"
landing on a different planner process (or after a restart) would find nothing.
This module persists it to Postgres (table `pending_confirmations`, created in
`db._SCHEMA_SQL`) so the resume works across processes.

The runtime keeps an in-memory cache and uses this as the durable source of truth,
rehydrating on a cache miss (same pattern as `conversation_store`). When no DB is
configured (Tier A/B laptop runs, unit tests) every function is a no-op and the
in-memory cache is the only store.
"""
from __future__ import annotations

import json
import os
from typing import Any

from planner_agent.main_agent.db import get_pool


def _enabled() -> bool:
    return bool(os.getenv("REGISTRATION_DB_DSN"))


async def save(context_id: str, owner_email: str, payload: dict[str, Any]) -> None:
    """Persist (or replace) the pending confirmation for a context_id."""
    if not _enabled():
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pending_confirmations (context_id, owner_email, payload) "
            "VALUES ($1, $2, $3::jsonb) "
            "ON CONFLICT (context_id) DO UPDATE SET "
            "owner_email = EXCLUDED.owner_email, payload = EXCLUDED.payload, created_at = now()",
            context_id,
            owner_email,
            json.dumps(payload),
        )


async def get(context_id: str) -> dict[str, Any] | None:
    """Return the persisted pending confirmation for a context_id, or None."""
    if not _enabled():
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM pending_confirmations WHERE context_id = $1",
            context_id,
        )
    if row is None:
        return None
    payload = row["payload"]
    # asyncpg returns JSONB as a str unless a codec is registered; decode defensively.
    return json.loads(payload) if isinstance(payload, str) else payload


async def owner(context_id: str) -> str | None:
    """Return the owner_email of a persisted pending confirmation, or None."""
    if not _enabled():
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
    if not _enabled():
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM pending_confirmations WHERE context_id = $1", context_id)
