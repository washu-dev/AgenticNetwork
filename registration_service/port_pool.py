"""Port pool manager — allocate and release ports for spawned agents."""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)

_PORT_RANGE_START = 19200
_PORT_RANGE_END = 19299


async def seed_ports(
    conn: asyncpg.Connection,
    start: int = _PORT_RANGE_START,
    end: int = _PORT_RANGE_END,
) -> int:
    """Insert port rows if the table is empty. Returns number of rows inserted."""
    existing = await conn.fetchval("SELECT COUNT(*) FROM port_pool")
    if existing:
        return 0
    rows = [(port, False, None) for port in range(start, end + 1)]
    await conn.executemany(
        "INSERT INTO port_pool (port, in_use, agent_id) VALUES ($1, $2, $3)",
        rows,
    )
    logger.info("port_pool_seeded start=%d end=%d count=%d", start, end, len(rows))
    return len(rows)


async def assign_port(conn: asyncpg.Connection, agent_id: str) -> int:
    """Claim a free port and associate it with agent_id.

    Raises RuntimeError if no free port is available.
    Uses SELECT FOR UPDATE SKIP LOCKED to be safe under concurrent requests.

    Also excludes any port already owned by an `agents` row: `agents.port` is
    UNIQUE and is the real source of truth, while `port_pool.in_use` is a
    secondary flag that can drift out of sync (e.g. after delete/re-register
    churn or a restart-time recovery that re-seats rows). Consulting the
    authoritative table here prevents handing out a port that's actually taken
    (which would 500 on the UNIQUE(port) constraint).
    """
    row = await conn.fetchrow(
        """
        SELECT port FROM port_pool
        WHERE in_use = false
          AND port NOT IN (SELECT port FROM agents WHERE port IS NOT NULL)
        ORDER BY port
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """,
    )
    if row is None:
        raise RuntimeError("No free ports available in pool")
    port: int = row["port"]
    await conn.execute(
        "UPDATE port_pool SET in_use = true, agent_id = $1 WHERE port = $2",
        agent_id,
        port,
    )
    logger.info("port_assigned port=%d agent_id=%s", port, agent_id)
    return port


async def release_port(conn: asyncpg.Connection, port: int) -> None:
    """Mark a port as free and clear its agent association."""
    await conn.execute(
        "UPDATE port_pool SET in_use = false, agent_id = NULL WHERE port = $1",
        port,
    )
    logger.info("port_released port=%d", port)
