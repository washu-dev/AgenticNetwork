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
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT NOT NULL,
    description       TEXT NOT NULL,
    handler_type      TEXT NOT NULL CHECK (handler_type IN ('llm', 'api_proxy', 'custom', 'endpoint_ops', 'tool_loop')),
    oasf_skills       JSONB NOT NULL,
    config            JSONB NOT NULL,
    port              INT UNIQUE,
    status            TEXT NOT NULL DEFAULT 'registered'
                          CHECK (status IN ('registered', 'live', 'failed', 'stopped', 'building', 'image_ready')),
    handler_file_path TEXT,
    approved          BOOLEAN NOT NULL DEFAULT TRUE,
    is_archive        BOOLEAN NOT NULL DEFAULT FALSE,
    endpoint_url      TEXT,
    ads_cid           TEXT,
    image_ref         TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent migrations for databases created before custom-handler support.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS handler_file_path TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS approved BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS is_archive BOOLEAN NOT NULL DEFAULT FALSE;
-- image_ref: the built custom-agent image's Docker Hub manifest digest
-- ('sha256:...'), captured at image_ready so CloudRunBackend can deploy by
-- digest instead of a floating ':latest' tag.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS image_ref TEXT;
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_handler_type_check;
ALTER TABLE agents ADD CONSTRAINT agents_handler_type_check
    CHECK (handler_type IN ('llm', 'api_proxy', 'custom', 'endpoint_ops', 'tool_loop'));
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_status_check;
ALTER TABLE agents ADD CONSTRAINT agents_status_check
    CHECK (status IN ('registered', 'live', 'failed', 'stopped', 'building', 'image_ready'));

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
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_db() first")
    return _pool  # type: ignore[return-value]


async def init_db() -> None:
    """Create connection pool and run schema migrations."""
    global _pool

    if os.getenv("FAKE_DB"):
        from registration_service.fake_db import get_fake_pool, init_fake_db
        await init_fake_db()
        _pool = get_fake_pool()  # type: ignore[assignment]
        logger.info("db_init mode=fake")
        return

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
    if _pool is None:
        return
    if os.getenv("FAKE_DB"):
        from registration_service.fake_db import close_fake_db
        await close_fake_db()
        _pool = None
        return
    await _pool.close()
    _pool = None


# ── Agent helper layer ──────────────────────────────────────────────────────────
# Used by the custom-handler code paths (upload / approve / reject / build poll).
# The existing llm/api_proxy paths in server.py keep their inline SQL unchanged.

import json as _json
import uuid as _uuid

_UPDATABLE_COLUMNS = {
    "status",
    "approved",
    "handler_file_path",
    "is_archive",
    "endpoint_url",
    "ads_cid",
    "port",
    "image_ref",
}


def _coerce_uuid(value: Any) -> "_uuid.UUID":
    return value if isinstance(value, _uuid.UUID) else _uuid.UUID(str(value))


async def create_agent(
    *,
    name: str,
    description: str,
    handler_type: str,
    oasf_skills: list[dict],
    config: dict,
    approved: bool = True,
) -> dict[str, Any]:
    """Insert a new agent row, assign a port, and return the row as a dict."""
    if os.getenv("FAKE_DB"):
        from registration_service.fake_db import fake_create_agent
        return fake_create_agent(
            name=name,
            description=description,
            handler_type=handler_type,
            oasf_skills=oasf_skills,
            config=config,
            approved=approved,
        )

    from registration_service.port_pool import assign_port

    pool = await get_pool()
    agent_id = _uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO agents
                    (id, name, description, handler_type, oasf_skills, config, status, approved)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, 'registered', $7)
                """,
                agent_id,
                name,
                description,
                handler_type,
                _json.dumps(oasf_skills),
                _json.dumps(config),
                approved,
            )
            port = await assign_port(conn, str(agent_id))
            await conn.execute(
                "UPDATE agents SET port = $1 WHERE id = $2", port, agent_id
            )
            row = await conn.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
    return dict(row)


async def get_agent(agent_id: Any) -> dict[str, Any] | None:
    if os.getenv("FAKE_DB"):
        from registration_service.fake_db import fake_get_agent
        return fake_get_agent(agent_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agents WHERE id = $1", _coerce_uuid(agent_id)
        )
    return dict(row) if row else None


async def update_agent(agent_id: Any, **fields: Any) -> None:
    """Update an agent's columns. Only whitelisted columns are accepted."""
    unknown = set(fields) - _UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"update_agent: unknown columns {sorted(unknown)}")
    if not fields:
        return

    if os.getenv("FAKE_DB"):
        from registration_service.fake_db import fake_update_agent
        fake_update_agent(agent_id, **fields)
        return

    set_clauses = []
    values: list[Any] = []
    for i, (col, val) in enumerate(fields.items(), start=1):
        set_clauses.append(f"{col} = ${i}")
        values.append(val)
    set_sql = ", ".join(set_clauses) + ", updated_at = now()"
    values.append(_coerce_uuid(agent_id))

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE agents SET {set_sql} WHERE id = ${len(values)}", *values
        )


async def list_agents_by_status(status: str) -> list[dict[str, Any]]:
    if os.getenv("FAKE_DB"):
        from registration_service.fake_db import fake_list_agents_by_status
        return fake_list_agents_by_status(status)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM agents WHERE status = $1 ORDER BY created_at DESC", status
        )
    return [dict(r) for r in rows]
