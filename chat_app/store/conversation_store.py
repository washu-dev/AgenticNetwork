"""Postgres-backed conversation history store (P4g/H2).

Conversation history used to live in a process-local SQLite file, which does not
survive a horizontally-scaled deployment. It now lives in the shared Postgres
database (table `conversation_messages`, created in `db._SCHEMA_SQL`) keyed by
`context_id` and stamped with the `owner_email` of the conversation.

Every save rewrites the full history for a context_id (the in-memory working
context is authoritative), but uses an UPSERT so each turn keeps its original
`created_at` — that's what `list_sessions` derives start/last-active times from.

When no database is configured (Tier A/B laptop runs, unit tests), every function
is a graceful no-op: history simply isn't persisted and the in-memory working
context serves the live session.
"""
from __future__ import annotations

import os

from chat_app.store.db import get_pool


def _enabled() -> bool:
    """Persistence is active only when a shared DB is configured (mirrors server.py)."""
    return bool(os.getenv("REGISTRATION_DB_DSN"))


def _parse_history(history: list[str]) -> list[tuple[str, str]]:
    """Convert ['user: ...', 'assistant: ...'] → [(role, text), ...]."""
    rows = []
    for entry in history:
        if entry.startswith("user: "):
            rows.append(("user", entry[6:]))
        elif entry.startswith("assistant: "):
            rows.append(("assistant", entry[11:]))
    return rows


async def save(context_id: str, history: list[str], owner_email: str) -> None:
    """Persist the full conversation history for a context_id under owner_email."""
    if not _enabled():
        return
    rows = _parse_history(history)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # UPSERT keeps created_at stable for existing turns (only new turns get
            # now()), so started_at/last_active_at derived in list_sessions are real.
            if rows:
                await conn.executemany(
                    "INSERT INTO conversation_messages "
                    "(context_id, turn_index, role, text, owner_email) "
                    "VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (context_id, turn_index) DO UPDATE SET "
                    "role = EXCLUDED.role, text = EXCLUDED.text, owner_email = EXCLUDED.owner_email",
                    [(context_id, i, role, text, owner_email) for i, (role, text) in enumerate(rows)],
                )
            # Drop any trailing turns if the history shrank.
            await conn.execute(
                "DELETE FROM conversation_messages WHERE context_id = $1 AND turn_index >= $2",
                context_id,
                len(rows),
            )


async def load(context_id: str) -> list[dict] | None:
    """Return [{role, text}, ...] or None if persistence is disabled."""
    if not _enabled():
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, text FROM conversation_messages "
            "WHERE context_id = $1 ORDER BY turn_index",
            context_id,
        )
    return [{"role": r["role"], "text": r["text"]} for r in rows]


async def delete(context_id: str) -> None:
    """Remove all messages for a context_id."""
    if not _enabled():
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversation_messages WHERE context_id = $1", context_id)


async def owner(context_id: str) -> str | None:
    """Return the owner_email of a persisted conversation, or None if unknown."""
    if not _enabled():
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT owner_email FROM conversation_messages WHERE context_id = $1 LIMIT 1",
            context_id,
        )
    return row["owner_email"] if row else None


async def load_history(context_id: str) -> list[str] | None:
    """Return history as ['user: ...', 'assistant: ...'] for rehydrating a ConversationContext."""
    messages = await load(context_id)
    if messages is None:
        return None
    return [f"{m['role']}: {m['text']}" for m in messages]


async def list_sessions(owner_email: str) -> list[dict]:
    """Return the caller's conversations, newest-active first (drives GET /sessions)."""
    if not _enabled():
        return []
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.context_id,
                   MIN(m.created_at) AS started_at,
                   MAX(m.created_at) AS last_active_at,
                   (SELECT text FROM conversation_messages t
                    WHERE t.context_id = m.context_id AND t.role = 'user'
                    ORDER BY t.turn_index LIMIT 1) AS title
            FROM conversation_messages m
            WHERE m.owner_email = $1
            GROUP BY m.context_id
            ORDER BY MAX(m.created_at) DESC
            """,
            owner_email,
        )
    return [
        {
            "context_id": r["context_id"],
            "title": (r["title"] or "")[:60],
            "started_at": r["started_at"].isoformat(),
            "last_active_at": r["last_active_at"].isoformat(),
        }
        for r in rows
    ]
