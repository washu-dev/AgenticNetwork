"""fake_db fidelity: the in-memory store must behave like postgres on the paths
the REAL Supervisor drives (FAKE_DB=1 FAKE_SUPERVISOR=0), and must fail loudly on
SQL it doesn't model instead of silently succeeding.
"""

from __future__ import annotations

import logging
import socket
import time
import uuid

import pytest

from registration_service import db
from registration_service.fake_db import get_fake_pool, reset_fake_db
from registration_service.port_pool import assign_port, port_free_on_host, release_port
from registration_service.tests.conftest import LLM_PAYLOAD



async def _fresh_db():
    reset_fake_db()
    db._pool = None
    await db.init_db()
    return await db.get_pool()


@pytest.mark.parametrize(
    "method,sql",
    [
        ("execute", "UPDATE agents SET name = $1 WHERE id = $2"),
        ("fetchrow", "SELECT name FROM agents WHERE id = $1"),
        ("fetch", "SELECT * FROM agents WHERE status = 'failed'"),
        ("fetchval", "SELECT max(port) FROM port_pool"),
    ],
)
async def test_unknown_sql_raises_and_logs_a_warning(method: str, sql: str, caplog) -> None:
    pool = await _fresh_db()
    async with pool.acquire() as conn:
        with caplog.at_level(logging.WARNING, logger="registration_service.fake_db"):
            with pytest.raises(NotImplementedError, match="fake_db does not handle"):
                await getattr(conn, method)(sql, uuid.uuid4(), uuid.uuid4())
    assert any("fake_db_unhandled_sql" in r.getMessage() for r in caplog.records)
    await db.close_db()


async def test_executemany_unknown_sql_raises() -> None:
    pool = await _fresh_db()
    async with pool.acquire() as conn:
        with pytest.raises(NotImplementedError):
            await conn.executemany("INSERT INTO agents (id) VALUES ($1)", [(1,)])
    await db.close_db()


async def test_select_1_for_readiness() -> None:
    pool = await _fresh_db()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1") == 1
    await db.close_db()


async def test_transaction_rolls_back_on_error() -> None:
    pool = await _fresh_db()
    agent_id = uuid.uuid4()
    async with pool.acquire() as conn:
        with pytest.raises(RuntimeError, match="boom"):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO agents (id, name, description, handler_type, oasf_skills, config, status) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, 'registered')",
                    agent_id, "Rolled Back", "d", "llm", "[]", "{}",
                )
                await assign_port(conn, str(agent_id))
                raise RuntimeError("boom")
    store = get_fake_pool()._store
    assert agent_id not in store.agents
    assert not any(info["in_use"] for info in store.port_pool.values())
    await db.close_db()


async def test_assign_port_skips_ports_held_by_agent_rows() -> None:
    """Mirrors the real query's `port NOT IN (SELECT port FROM agents ...)`:
    a row can keep its port after the pool flag was cleared (restart)."""
    pool = await _fresh_db()
    agent = await db.create_agent(
        name="Holder", description="d", handler_type="llm",
        oasf_skills=[], config={}, approved=True,
    )
    held = agent["port"]
    async with pool.acquire() as conn:
        await release_port(conn, held)  # pool says free; the agents row still holds it
        port = await assign_port(conn, str(uuid.uuid4()))
    assert port != held
    await db.close_db()


async def test_assign_port_skips_port_busy_on_host() -> None:
    pool = await _fresh_db()
    store = get_fake_pool()._store
    lowest = store.free_ports()[0]
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", lowest))
        squatter.listen()
        assert not port_free_on_host(lowest)
        async with pool.acquire() as conn:
            port = await assign_port(conn, str(uuid.uuid4()))
        assert port != lowest
        # The busy port is skipped, not burned: it stays free in the pool.
        assert store.port_pool[lowest]["in_use"] is False
        # db.create_agent's fake path applies the same rule.
        agent = await db.create_agent(
            name="Uploaded", description="d", handler_type="custom",
            oasf_skills=[], config={}, approved=False,
        )
        assert agent["port"] not in (lowest, port)
    assert port_free_on_host(lowest)
    await db.close_db()


async def test_register_503_leaves_no_half_registered_row(client) -> None:
    """With every port taken the register call 503s inside its transaction; the
    INSERT must roll back, or the name would 409 forever after."""
    store = get_fake_pool()._store
    for info in store.port_pool.values():
        info["in_use"] = True
    r = await client.post("/agents", json=LLM_PAYLOAD)
    assert r.status_code == 503
    assert (await client.get("/agents")).json() == []

    for info in store.port_pool.values():
        info["in_use"] = False
    r2 = await client.post("/agents", json=LLM_PAYLOAD)
    assert r2.status_code == 201, r2.text


# ── port_free_on_host: what an agent's bind would see ────────────────────────────


def _listener(host: str) -> socket.socket:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn binds
    sock.bind((host, 0))
    sock.listen()
    return sock


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1"])
def test_port_with_a_listener_is_busy(host: str) -> None:
    with _listener(host) as sock:
        assert not port_free_on_host(sock.getsockname()[1])


def test_time_wait_leftovers_do_not_make_a_port_busy() -> None:
    """Regression: a plain bind (no SO_REUSEADDR) failed on the TIME_WAIT sockets
    a stopped agent leaves behind for its health-probe connections, so every
    freed port looked taken for a minute while an agent could bind it fine."""
    sock = _listener("0.0.0.0")
    port = sock.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    accepted, _ = sock.accept()
    accepted.close()  # the server side closes first -> its end sits in TIME_WAIT
    time.sleep(0.05)
    client.close()
    sock.close()
    assert port_free_on_host(port)
