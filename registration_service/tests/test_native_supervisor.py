"""The REAL Supervisor on the in-memory store: FAKE_DB=1 FAKE_SUPERVISOR=0.

This is the native dev stack's mode: no postgres, but real agent processes
(SubprocessBackend) published through dirctl (here the dev stack's fake). The
first test drives one llm agent through every supervisor path that touches the
store — register, spawn, publish, live, health-loop restart, manual restart,
shutdown, startup recovery, delete — and calls it over A2A on the way, so a
query fake_db doesn't model (it raises) or a wrong lifecycle step fails here.

The rest pin down the unpublish-before-respawn rule and the health-loop restart
bookkeeping with a stub backend and publisher (no processes).
"""

from __future__ import annotations

import asyncio
import os
import signal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytest.importorskip("a2a")
pytest.importorskip("uvicorn")

import registration_service.db as db_mod  # noqa: E402
import registration_service.supervisor as sup_mod  # noqa: E402
from ads_utils.publisher import DirectoryUnavailable  # noqa: E402
from registration_service.fake_db import reset_fake_db  # noqa: E402
from registration_service.port_pool import port_free_on_host  # noqa: E402
from registration_service.supervisor import Supervisor  # noqa: E402
from registration_service.tests.agent_stubs import (  # noqa: E402
    a2a_call,
    fake_dirctl_env,
    pid_alive,
    routed_cids,
    stub_openai,
)

LLM_AGENT = {
    "name": "Native Summarizer",
    "description": "Summarizes text",
    "handler_type": "llm",
    "oasf_skills": [{"name": "nlp/text-summarization", "id": 10101, "key": "nlp/text-summarization"}],
    "config": {"system_prompt": "Be brief.", "model": "stub-model", "tools": {"source": "none"}},
}


async def _wait_until(predicate, seconds: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def _fresh_store() -> None:
    reset_fake_db()
    db_mod._pool = None
    sup_mod._supervisor = None
    await db_mod.init_db()


async def _teardown() -> None:
    sup = sup_mod._supervisor
    if sup is not None:
        await sup.stop_health_loop()
        await sup.shutdown()
    await db_mod.close_db()
    reset_fake_db()
    db_mod._pool = None
    sup_mod._supervisor = None


@pytest_asyncio.fixture
async def native(monkeypatch, tmp_path):
    dirctl_env, state = fake_dirctl_env(tmp_path)
    for name, value in dirctl_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FAKE_DB", "1")
    monkeypatch.setenv("FAKE_SUPERVISOR", "0")
    monkeypatch.setenv("SPAWN_BACKEND", "subprocess")
    monkeypatch.setenv("ADS_SERVER_ADDR", "fake-dir:8888")
    monkeypatch.setenv("ADS_ROUTING_PUBLISH_POLL_SECONDS", "0.05")
    monkeypatch.delenv("PYTHONPATH", raising=False)  # agents must import the repo themselves
    with stub_openai() as (llm_url, llm_requests):
        monkeypatch.setenv("OPENAI_BASE_URL", llm_url)
        monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
        await _fresh_store()
        sup = sup_mod.get_supervisor()
        assert type(sup) is Supervisor
        from registration_service.server import app

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                yield SimpleNamespace(client=client, state=state, llm_requests=llm_requests)
        finally:
            await _teardown()


def _pid(agent_id: str) -> int:
    return sup_mod._supervisor._backend._procs[agent_id].pid


async def _ask(endpoint_url: str, text: str) -> None:
    for stream in (False, True):
        result = await a2a_call(endpoint_url, text, stream=stream)
        assert result["state"] == "completed", result
        assert result["text"] == f"stub-llm says: {text}"


async def test_llm_agent_through_every_supervisor_path(native, monkeypatch) -> None:
    client, state = native.client, native.state

    # Register: spawned, published, live.
    r = await client.post("/agents", json=LLM_AGENT)
    assert r.status_code == 201, r.text
    agent = r.json()
    agent_id, port = agent["id"], agent["port"]
    assert agent["status"] == "live"
    assert agent["endpoint_url"] == f"http://localhost:{port}/"
    assert agent["ads_cid"] and routed_cids(state) == [agent["ads_cid"]]
    await _ask(agent["endpoint_url"], "hello")
    assert native.llm_requests, "the agent never called the LLM"
    ready = (await client.get("/ready")).json()
    assert ready["ready"] is True and ready["checks"]["directory"]["status"] == "ok"

    # Health-loop restart after a crash: the old card goes before the new one.
    sup = sup_mod._supervisor
    first_pid, first_cid = _pid(agent_id), agent["ads_cid"]
    os.killpg(first_pid, signal.SIGKILL)
    assert await _wait_until(lambda: sup._backend._procs[agent_id].returncode is not None)
    monkeypatch.setattr(sup_mod, "_BACKOFF_BASE_SECONDS", 0.0)
    await sup._check_all_live_agents()
    restart = sup._restart_tasks[agent_id]
    await sup._check_all_live_agents()  # still dead and restarting: no second restart
    assert sup._restart_tasks == {agent_id: restart}
    await restart
    agent = (await client.get(f"/agents/{agent_id}")).json()
    assert agent["status"] == "live" and agent["port"] == port
    assert agent["ads_cid"] != first_cid and routed_cids(state) == [agent["ads_cid"]]
    assert _pid(agent_id) != first_pid
    assert sup._restart_tasks == {}
    await _ask(agent["endpoint_url"], "after crash")

    # Manual restart: stop + unpublish, new port, respawn + publish.
    old_pid, old_port = _pid(agent_id), port
    r = await client.post(f"/agents/{agent_id}/restart")
    assert r.status_code == 200, r.text
    agent = r.json()
    assert agent["status"] == "live" and routed_cids(state) == [agent["ads_cid"]]
    assert not pid_alive(old_pid) and port_free_on_host(old_port)
    await _ask(agent["endpoint_url"], "after restart")

    # Service shutdown: children stopped, cards dropped, rows kept 'live'.
    pid, port = _pid(agent_id), agent["port"]
    await sup.stop_health_loop()
    await sup.shutdown()
    assert await _wait_until(lambda: not pid_alive(pid))
    assert port_free_on_host(port)
    assert routed_cids(state) == []
    row = await db_mod.get_agent(agent_id)
    assert row["status"] == "live" and row["ads_cid"] is None

    # Startup recovery (a new supervisor on the same store) respawns + republishes.
    sup_mod._supervisor = Supervisor()
    await sup_mod._supervisor.recover_live_agents()
    agent = (await client.get(f"/agents/{agent_id}")).json()
    assert agent["status"] == "live" and routed_cids(state) == [agent["ads_cid"]]
    await _ask(agent["endpoint_url"], "after recovery")

    # Delete: unpublished, stopped, port free, row gone.
    pid = _pid(agent_id)
    assert (await client.delete(f"/agents/{agent_id}")).status_code == 204
    assert routed_cids(state) == []
    assert await _wait_until(lambda: not pid_alive(pid))
    assert port_free_on_host(agent["port"])
    assert (await client.get(f"/agents/{agent_id}")).status_code == 404
    assert await sup_mod._supervisor.shutdown() is None  # nothing left to stop


# ── Unpublish-before-respawn and restart bookkeeping (no processes) ──────────────


class _Backend:
    def __init__(self) -> None:
        self.spawned: list[str] = []
        self.alive: set[str] = set()

    async def spawn(self, *, agent_id, **_):
        self.spawned.append(agent_id)
        self.alive.add(agent_id)

    async def stop(self, agent_id):
        self.alive.discard(agent_id)

    async def stop_all(self):
        stopped = sorted(self.alive)
        self.alive.clear()
        return stopped

    async def is_alive(self, agent_id):
        return agent_id in self.alive


class _Publisher:
    """Records directory calls in order. ``cid`` is what publish returns (the
    real CID is a content hash, so an unchanged card gets the same one)."""

    def __init__(self, cid: str = "cid-1", *, up: bool = True) -> None:
        self.cid, self.up = cid, up
        self.calls: list[tuple[str, str]] = []

    def publish(self, card, advertise_url=None):
        if not self.up:
            raise DirectoryUnavailable("directory down")
        self.calls.append(("publish", self.cid))
        return SimpleNamespace(backend_ref=self.cid)

    def unpublish(self, cid):
        self.calls.append(("unpublish", cid))
        return self.up  # DirectoryPublisher.unpublish: False when the call failed


async def _live_agent(publisher: _Publisher) -> tuple[Supervisor, str]:
    await _fresh_store()
    backend = _Backend()
    sup = Supervisor(backend=backend, publisher=publisher)
    sup_mod._supervisor = sup
    row = await db_mod.create_agent(
        name=LLM_AGENT["name"], description="d", handler_type="llm",
        oasf_skills=LLM_AGENT["oasf_skills"], config=LLM_AGENT["config"], approved=True,
    )
    agent_id = str(row["id"])
    await sup.spawn_and_publish(agent_id)
    return sup, agent_id


@pytest.fixture
async def store():
    yield
    await _teardown()


async def test_respawn_unpublishes_the_old_card_before_publishing(store) -> None:
    """With an unchanged card the new CID equals the old one; unpublishing
    after the publish would have removed the card just published."""
    publisher = _Publisher(cid="same-cid")
    sup, agent_id = await _live_agent(publisher)
    publisher.calls.clear()
    await sup.spawn_and_publish(agent_id)  # what restart and recovery call
    assert publisher.calls == [("unpublish", "same-cid"), ("publish", "same-cid")]
    row = await db_mod.get_agent(agent_id)
    assert row["status"] == "live" and row["ads_cid"] == "same-cid"


async def test_respawn_with_directory_down_keeps_the_cid(store) -> None:
    publisher = _Publisher(cid="old-cid")
    sup, agent_id = await _live_agent(publisher)
    publisher.up = False
    await sup.spawn_and_publish(agent_id)
    row = await db_mod.get_agent(agent_id)
    # The old card is still in the directory, so the row still points at it.
    assert row["status"] == "live" and row["ads_cid"] == "old-cid"
    assert sup._backend.alive == {agent_id}


async def test_shutdown_clears_the_cid_only_when_unpublished(store) -> None:
    publisher = _Publisher(cid="cid-1")
    sup, agent_id = await _live_agent(publisher)
    publisher.up = False
    await sup.shutdown()
    assert (await db_mod.get_agent(agent_id))["ads_cid"] == "cid-1"  # recovery retries

    publisher.up = True
    await sup.spawn_and_publish(agent_id)
    await sup.shutdown()
    row = await db_mod.get_agent(agent_id)
    assert row["status"] == "live" and row["ads_cid"] is None
    assert publisher.calls[-1] == ("unpublish", "cid-1")


async def test_health_loop_restarts_once_and_shutdown_cancels_it(store, monkeypatch) -> None:
    sup, agent_id = await _live_agent(_Publisher())
    sup._backend.alive.clear()  # crashed
    monkeypatch.setattr(sup_mod, "_BACKOFF_BASE_SECONDS", 60.0)  # stays in backoff
    await sup._check_all_live_agents()
    await sup._check_all_live_agents()
    assert list(sup._restart_tasks) == [agent_id]
    task = sup._restart_tasks[agent_id]

    await sup.stop_health_loop()
    assert task.cancelled() and sup._restart_tasks == {}
    assert sup._backend.spawned == [agent_id]  # only the original spawn
