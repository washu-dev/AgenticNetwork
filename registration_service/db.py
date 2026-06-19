"""Database connection pool and schema migrations."""

from __future__ import annotations

import logging
import os
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL,
    description   TEXT NOT NULL,
    handler_type  TEXT NOT NULL CHECK (handler_type IN ('llm', 'api_proxy')),
    oasf_skills   JSONB NOT NULL,
    config        JSONB NOT NULL,
    port          INT UNIQUE,
    status        TEXT NOT NULL DEFAULT 'registered'
                      CHECK (status IN ('registered', 'live', 'failed', 'stopped')),
    endpoint_url  TEXT,
    ads_cid       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS port_pool (
    port      INT PRIMARY KEY,
    in_use    BOOLEAN NOT NULL DEFAULT false,
    agent_id  UUID REFERENCES agents(id)
);

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'agents_updated_at'
    ) THEN
        CREATE TRIGGER agents_updated_at
            BEFORE UPDATE ON agents
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
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
    password = os.getenv("PGPASSWORD", "postgres")
    dbname = os.getenv("PGDATABASE", "registration")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_db() first")
    return _pool


async def init_db() -> None:
    """Create connection pool and run schema migrations."""
    global _pool
    dsn = _dsn()
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
        from registration_service.port_pool import seed_ports
        await seed_ports(conn)
        # Clean up stopped/failed agents from previous runs so ports are freed.
        await conn.execute(
            "UPDATE port_pool SET in_use = false, agent_id = NULL "
            "WHERE agent_id IN (SELECT id FROM agents WHERE status IN ('stopped', 'failed'))"
        )
        await conn.execute("DELETE FROM agents WHERE status IN ('stopped', 'failed')")
    logger.info("db_init dsn=%s", dsn.split("@")[-1])


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
