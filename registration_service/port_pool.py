"""Port pool manager — allocate and release ports for spawned agents."""

from __future__ import annotations

import logging
import os
import socket

import asyncpg

logger = logging.getLogger(__name__)

# Dynamic port range for spawned custom agents. Configurable so an isolated test
# instance can offset its range and not collide with prod's on a shared node —
# submit.sbatch sets CUSTOM_AGENT_PORT_START/END for test. See docs/MULTI_CONTRIBUTOR.md.
_PORT_RANGE_START = int(os.getenv("CUSTOM_AGENT_PORT_START", "19200"))
_PORT_RANGE_END = int(os.getenv("CUSTOM_AGENT_PORT_END", "19299"))


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


def port_free_on_host(port: int) -> bool:
    """True if an agent could bind ``port`` and no other listener holds it.

    The DB only knows which ports it handed out; an orphaned agent from an earlier
    run or an unrelated program can still hold one, and the new agent would then
    fail to bind while the old listener answered its readiness probe.

    Agents (uvicorn) bind 0.0.0.0 with SO_REUSEADDR and the readiness probe goes
    to 127.0.0.1, so test both addresses that way. SO_REUSEADDR matters: a plain
    bind also fails on TIME_WAIT leftovers of closed connections, which an agent's
    bind ignores, so every port would look busy for a minute after its agent
    stopped. With it, a bind still fails on a listener (on Linux, any listener on
    the port; on macOS, one on the same address, hence both addresses).
    """
    for host in ("0.0.0.0", "127.0.0.1"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                return False
    return True


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

    A port the DB considers free but the OS reports in use is skipped (and stays
    free in the pool, so it is retried once its holder exits).
    """
    busy: list[int] = []
    while True:
        row = await conn.fetchrow(
            """
            SELECT port FROM port_pool
            WHERE in_use = false
              AND port NOT IN (SELECT port FROM agents WHERE port IS NOT NULL)
              AND NOT (port = ANY($1::int[]))
            ORDER BY port
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            busy,
        )
        if row is None:
            if busy:
                raise RuntimeError(
                    f"No free ports available in pool ({len(busy)} skipped: in use on this host)"
                )
            raise RuntimeError("No free ports available in pool")
        port: int = row["port"]
        if port_free_on_host(port):
            break
        logger.warning("port_busy_on_host port=%d agent_id=%s skipping", port, agent_id)
        busy.append(port)
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
