"""Database connection pool and schema for users, sessions, and credentials."""

from __future__ import annotations

import logging
import os

import asyncpg

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


def _dsn() -> str:
    dsn = os.getenv("REGISTRATION_DB_DSN")
    if dsn:
        return dsn
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    user = os.getenv("PGUSER", "postgres")
    dbname = os.getenv("PGDATABASE", "registration")
    return f"postgresql://{user}@{host}:{port}/{dbname}"


async def get_pool() -> asyncpg.Pool:
    global _pool
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


async def fetch_user_credentials(user_id: object) -> dict:
    """Return a dict of {provider: decrypted_payload} for all stored credentials."""
    from planner_agent.main_agent.crypto import decrypt

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


async def close_db() -> None:
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
