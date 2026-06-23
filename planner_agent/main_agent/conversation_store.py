"""SQLite-backed conversation history store.

Uses stdlib sqlite3 wrapped in run_in_executor so it fits the async server
without adding any new dependencies. The DB path is configured via
MAIN_AGENT_CONVERSATION_DB (defaults to /tmp/planner_agent_conversations.db).

Each row is one message (user or assistant) in a conversation. We replace the
full history for a context_id on every save so the record stays authoritative.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

_DB_PATH: Path | None = None
_executor_lock = asyncio.Lock()

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    context_id  TEXT    NOT NULL,
    turn_index  INTEGER NOT NULL,
    role        TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    PRIMARY KEY (context_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_conv_ctx ON conversation_messages(context_id);
"""


def init(path: str | Path) -> None:
    """Create the DB file and schema. Call once at server startup."""
    global _DB_PATH
    _DB_PATH = Path(path)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_DB_PATH) as conn:
        conn.executescript(_CREATE_SQL)


def _parse_history(history: list[str]) -> list[tuple[str, str]]:
    """Convert ['user: ...', 'assistant: ...'] → [(role, text), ...]."""
    rows = []
    for entry in history:
        if entry.startswith("user: "):
            rows.append(("user", entry[6:]))
        elif entry.startswith("assistant: "):
            rows.append(("assistant", entry[11:]))
    return rows


def _sync_save(context_id: str, rows: list[tuple[str, str]]) -> None:
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute("DELETE FROM conversation_messages WHERE context_id = ?", (context_id,))
        conn.executemany(
            "INSERT INTO conversation_messages VALUES (?, ?, ?, ?)",
            [(context_id, i, role, text) for i, (role, text) in enumerate(rows)],
        )
        conn.commit()


def _sync_load(context_id: str) -> list[dict]:
    with sqlite3.connect(_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT role, text FROM conversation_messages "
            "WHERE context_id = ? ORDER BY turn_index",
            (context_id,),
        ).fetchall()
    return [{"role": r[0], "text": r[1]} for r in rows]


async def save(context_id: str, history: list[str]) -> None:
    """Persist the full conversation history for a context_id."""
    if not _DB_PATH:
        return
    rows = _parse_history(history)
    loop = asyncio.get_running_loop()
    async with _executor_lock:
        await loop.run_in_executor(None, _sync_save, context_id, rows)


def _sync_delete(context_id: str) -> None:
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute("DELETE FROM conversation_messages WHERE context_id = ?", (context_id,))
        conn.commit()


async def delete(context_id: str) -> None:
    """Remove all messages for a context_id."""
    if not _DB_PATH:
        return
    loop = asyncio.get_running_loop()
    async with _executor_lock:
        await loop.run_in_executor(None, _sync_delete, context_id)


async def load(context_id: str) -> list[dict] | None:
    """Return [{role, text}, ...] or None if the DB is not initialised."""
    if not _DB_PATH:
        return None
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_load, context_id)
