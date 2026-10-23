"""Database connection pool and schema migrations."""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import asyncpg

from registration_service.flags import fake_db_enabled

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT NOT NULL,
    description       TEXT NOT NULL,
    -- Canonical types are ('llm', 'proxy', 'custom'); the other three are legacy
    -- names still present in rows written before the 2026-07 consolidation and
    -- still accepted on the wire (models.LEGACY_HANDLER_ALIASES). Old rows are
    -- normalized on READ (models.parse_config), not rewritten, so the constraint
    -- must keep permitting them.
    handler_type      TEXT NOT NULL CHECK (handler_type IN
                          ('llm', 'proxy', 'custom',
                           'api_proxy', 'endpoint_ops', 'tool_loop')),
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
    build_commit      TEXT,
    resources         JSONB,
    owner_email       TEXT,
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
-- build_commit: the agent-directory commit an approval's build context went into.
-- The build workflow tags the image with that commit's SHA, and the build poll,
-- the cluster spawn and the Cloud Run deploy use that tag (or its digest), so an
-- agent runs the image built for its own approval, not whatever ':latest' holds.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS build_commit TEXT;
-- owner_email: the SSO identity that registered the agent (lowercased). Only it or
-- an admin may delete, restart or upload code to the agent; a row without one
-- (registered without an identity, or before this column) is admin-only. No API
-- changes it; an admin can set it with UPDATE.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_email TEXT;
-- resources: optional deploy sizing declared at registration (cpu/memory/
-- concurrency/max_instances/gpu) that cloud backends map to service parameters.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS resources JSONB;
-- Handler-type consolidation (2026-07): canonical ('llm','proxy','custom') plus the
-- three legacy names. Widening rather than rewriting is deliberate — an existing row
-- keeps its old handler_type AND its old config JSON, and both are normalized on read
-- (models.parse_config). Rewriting handler_type in SQL without also rewriting the
-- config JSON would produce rows that no longer parse, so we don't.
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_handler_type_check;
ALTER TABLE agents ADD CONSTRAINT agents_handler_type_check
    CHECK (handler_type IN ('llm', 'proxy', 'custom',
                            'api_proxy', 'endpoint_ops', 'tool_loop'));
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


# Dead agents the startup purge deletes. Spared: a 'failed' agent that built (has
# image_ref) — the build succeeded and only the deploy step failed, which is
# retryable on recovery (supervisor._recover_cloud_agents); and any row that still
# holds an ads_cid — its card is still in the directory and the CID is the only
# handle to remove it (recovery retries, then clears it). fake_db._is_purgeable
# mirrors this.
_PURGEABLE_SQL = (
    "(status = 'stopped' OR (status = 'failed' AND image_ref IS NULL)) AND ads_cid IS NULL"
)


async def init_db() -> None:
    """Create connection pool and run schema migrations."""
    global _pool

    if fake_db_enabled():
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
        # Clean up dead agents from previous runs so ports are freed.
        await conn.execute(
            "UPDATE port_pool SET in_use = false, agent_id = NULL "
            f"WHERE agent_id IN (SELECT id FROM agents WHERE {_PURGEABLE_SQL})"
        )
        await conn.execute(f"DELETE FROM agents WHERE {_PURGEABLE_SQL}")
    logger.info("db_init dsn=%s", dsn.split("@")[-1])


async def close_db() -> None:
    global _pool
    if _pool is None:
        return
    if fake_db_enabled():
        from registration_service.fake_db import reset_fake_db
        reset_fake_db()
        _pool = None
        return
    await _pool.close()
    _pool = None


# ── Agent helper layer ──────────────────────────────────────────────────────────

_UPDATABLE_COLUMNS = {
    "status",
    "approved",
    "handler_file_path",
    "is_archive",
    "endpoint_url",
    "ads_cid",
    "port",
    "image_ref",
    "build_commit",
}


def _coerce_uuid(value: Any) -> "uuid.UUID":
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def create_agent(
    *,
    name: str,
    description: str,
    handler_type: str,
    oasf_skills: list[dict],
    config: dict,
    approved: bool = True,
    resources: dict | None = None,
    owner_email: str | None = None,
) -> dict[str, Any]:
    """Insert a new agent row, assign a port, and return the row as a dict."""
    if fake_db_enabled():
        from registration_service.fake_db import fake_create_agent
        return fake_create_agent(
            name=name,
            description=description,
            handler_type=handler_type,
            oasf_skills=oasf_skills,
            config=config,
            approved=approved,
            resources=resources,
            owner_email=owner_email,
        )

    from registration_service.port_pool import assign_port

    pool = await get_pool()
    agent_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO agents
                    (id, name, description, handler_type, oasf_skills, config, status, approved,
                     resources, owner_email)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, 'registered', $7, $8::jsonb, $9)
                """,
                agent_id,
                name,
                description,
                handler_type,
                json.dumps(oasf_skills),
                json.dumps(config),
                approved,
                json.dumps(resources) if resources is not None else None,
                owner_email,
            )
            port = await assign_port(conn, str(agent_id))
            await conn.execute(
                "UPDATE agents SET port = $1 WHERE id = $2", port, agent_id
            )
            row = await conn.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
    return dict(row)


async def get_agent(agent_id: Any) -> dict[str, Any] | None:
    if fake_db_enabled():
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

    if fake_db_enabled():
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
    if fake_db_enabled():
        from registration_service.fake_db import fake_list_agents_by_status
        return fake_list_agents_by_status(status)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM agents WHERE status = $1 ORDER BY created_at DESC", status
        )
    return [dict(r) for r in rows]
