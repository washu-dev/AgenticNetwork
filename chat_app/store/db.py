"""Database connection pool and schema for users, sessions, and credentials."""

from __future__ import annotations

import logging
import os

import asyncpg

from agentnet.settings import database_configured, database_url

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_credentials (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,
    encrypted    TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, provider)
);

-- P4c (Option A): the provider column now stores canonical catalog `type`
-- strings. Self-healing migration of legacy values written before P4c — idempotent
-- (only rewrites the known legacy set; google_oauth is already canonical, untouched).
UPDATE user_credentials SET provider = 'openai_api_key'      WHERE provider = 'openai';
UPDATE user_credentials SET provider = 'anthropic_api_key'   WHERE provider = 'anthropic';
UPDATE user_credentials SET provider = 'brave_api_key'       WHERE provider = 'brave';
UPDATE user_credentials SET provider = 'google_api_key'      WHERE provider = 'google_api';
UPDATE user_credentials SET provider = 'google_maps_api_key' WHERE provider = 'google_maps';

-- P4c: per-agent credential grants. A grant records that the user consented to
-- inject a given credential type into a specific agent's requests.
CREATE TABLE IF NOT EXISTS agent_credential_grants (
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,
    cred_type  TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, agent_id, cred_type)
);

-- P4g/H2: conversation history moved off process-local SQLite into Postgres so it
-- survives restarts and is shared across chat app processes. owner_email keys on the
-- Shib identity (same as the in-memory working context / sessions) rather than a
-- users(id) FK, so anon/demo-user (no users row) can still persist in dev.
CREATE TABLE IF NOT EXISTS conversation_messages (
    context_id   TEXT        NOT NULL,
    turn_index   INT         NOT NULL,
    role         TEXT        NOT NULL,
    text         TEXT        NOT NULL,
    owner_email  TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (context_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_conv_owner ON conversation_messages(owner_email);

-- P4g/H2b: pending write/credential-consent confirmations, keyed by context_id, so a
-- prompt raised on one chat app process can be resumed on another (or after a restart).
-- payload is the same JSON dict the in-memory store held. Superseded per context.
CREATE TABLE IF NOT EXISTS pending_confirmations (
    context_id   TEXT        PRIMARY KEY,
    owner_email  TEXT        NOT NULL,
    payload      JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Set when an answer claims the row (pending_store.claim); the row is deleted once the
-- agent call or the cancel is done. While set, other answers are told it's in progress,
-- until the claim expires (pending_store.CLAIM_TTL_SECONDS).
-- claim_id names the answer holding the claim, so only that answer refreshes, releases
-- or deletes it.
-- released_claim_ids lists the claims given back unused for the current prompt
-- (pending_store.release); a claim statement under one of them that reaches the row only
-- afterwards takes nothing.
ALTER TABLE pending_confirmations
    ADD COLUMN IF NOT EXISTS claimed_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claim_id           TEXT,
    ADD COLUMN IF NOT EXISTS released_claim_ids TEXT[];

-- Old name kept on purpose: it already exists in deployed databases.
CREATE OR REPLACE FUNCTION planner_update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'user_credentials_updated_at'
    ) THEN
        CREATE TRIGGER user_credentials_updated_at
            BEFORE UPDATE ON user_credentials
            FOR EACH ROW EXECUTE FUNCTION planner_update_updated_at();
    END IF;
END;
$$;
"""


def configured() -> bool:
    """Whether a database is configured (DATABASE_URL, or the old REGISTRATION_DB_DSN).

    The chat app's one switch for DB mode: with it, users, history, credentials and
    pending confirmations live in Postgres and a request without an SSO identity is
    a 401 (unless CHAT_APP_ALLOW_ANON=1); without it, the app runs DB-less and an
    anonymous request becomes ``demo-user``.
    """
    return database_configured()


def _dsn() -> str:
    dsn = database_url()
    if dsn:
        return dsn
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    user = os.getenv("PGUSER", "postgres")
    dbname = os.getenv("PGDATABASE", "registration")
    return f"postgresql://{user}@{host}:{port}/{dbname}"


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_db() first")
    return _pool  # type: ignore[return-value]


async def init_db() -> None:
    """Create connection pool and run schema migrations."""
    global _pool
    dsn = _dsn()
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    logger.info("planner_db_init dsn=%s", dsn.split("@")[-1])


async def ping_db() -> None:
    """Run a trivial query; raises if the pool is missing or the DB is unreachable."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")


async def fetch_user_credentials(user_id: object) -> dict:
    """Return a dict of {provider: decrypted_payload} for all stored credentials."""
    from chat_app.store.crypto import decrypt

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT provider, encrypted FROM user_credentials WHERE user_id = $1",
            user_id,
        )
    result = {}
    for row in rows:
        try:
            result[row["provider"]] = decrypt(row["encrypted"])
        except Exception:
            logger.warning("planner_db_credential_decrypt_failed provider=%s", row["provider"])
    return result


async def list_grants(user_id: object, agent_id: str) -> set[str]:
    """Return the set of cred_types the user has granted to a specific agent (P4c)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT cred_type FROM agent_credential_grants WHERE user_id = $1 AND agent_id = $2",
            user_id,
            agent_id,
        )
    return {r["cred_type"] for r in rows}


async def list_all_grants(user_id: object) -> list[dict]:
    """Return all of the user's grants across agents (for the Settings UI)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT agent_id, cred_type, granted_at FROM agent_credential_grants "
            "WHERE user_id = $1 ORDER BY agent_id, cred_type",
            user_id,
        )
    return [
        {"agent_id": r["agent_id"], "cred_type": r["cred_type"], "granted_at": r["granted_at"].isoformat()}
        for r in rows
    ]


async def add_grant(user_id: object, agent_id: str, cred_type: str) -> None:
    """Record a per-agent credential grant (idempotent)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_credential_grants (user_id, agent_id, cred_type) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            user_id,
            agent_id,
            cred_type,
        )


async def revoke_grant(user_id: object, agent_id: str, cred_type: str) -> bool:
    """Remove a grant. Returns True if a row was deleted."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM agent_credential_grants WHERE user_id = $1 AND agent_id = $2 AND cred_type = $3",
            user_id,
            agent_id,
            cred_type,
        )
    return result != "DELETE 0"


async def close_db() -> None:
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
