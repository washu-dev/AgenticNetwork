"""Tests for the OASF-routable-skill guard in Supervisor.spawn_and_publish.

The API layer (server.register_agent) already rejects an unroutable skill list
before it reaches the DB, but this guard is the last line of defense for any row
that reaches spawn_and_publish some other way (a stale row from before the API
check existed, the cluster host-loop path, a future CloudRunBackend). It must
fire *before* the backend's spawn() is called — no orphaned process/container.
"""

from __future__ import annotations

import pytest

from registration_service import db
from registration_service.fake_db import reset_fake_db
from registration_service.supervisor import Supervisor

pytestmark = pytest.mark.asyncio


class _FakeBackend:
    def __init__(self) -> None:
        self.spawned: list[dict] = []

    async def spawn(self, **kwargs) -> None:
        self.spawned.append(kwargs)

    async def stop(self, agent_id: str) -> None:
        pass

    async def is_alive(self, agent_id: str) -> bool:
        return True


class _FakePublisher:
    class _Rec:
        backend_ref = "fake-cid"

    def publish(self, card, advertise_url=None):
        return self._Rec()

    def unpublish(self, cid):
        pass


async def _fresh_db():
    reset_fake_db()
    db._pool = None
    await db.init_db()


async def test_spawn_rejects_no_routable_skill_before_backend_spawn(monkeypatch):
    monkeypatch.delenv("SPAWN_BACKEND", raising=False)
    await _fresh_db()
    agent = await db.create_agent(
        name="Unroutable Agent",
        description="Has only a freeform skill",
        handler_type="llm",
        oasf_skills=[{"name": "custom/freeform-skill"}],  # no id anywhere
        config={"system_prompt": "hi", "model": "openai/gpt-oss-20b"},
        approved=True,
    )

    backend = _FakeBackend()
    sup = Supervisor(backend=backend, publisher=_FakePublisher())
    with pytest.raises(RuntimeError, match="no OASF skill with an id"):
        await sup.spawn_and_publish(str(agent["id"]))

    got = await db.get_agent(agent["id"])
    assert got["status"] == "failed"
    assert backend.spawned == [], "backend must not spawn a doomed-to-be-invisible agent"
    await db.close_db()


async def test_spawn_succeeds_with_one_routable_skill(monkeypatch):
    monkeypatch.delenv("SPAWN_BACKEND", raising=False)
    await _fresh_db()
    agent = await db.create_agent(
        name="Mixed Skills Agent",
        description="One freeform + one routable skill",
        handler_type="llm",
        oasf_skills=[
            {"name": "custom/freeform-skill"},
            {"name": "nlp/text-summarization", "id": 10101},
        ],
        config={"system_prompt": "hi", "model": "openai/gpt-oss-20b"},
        approved=True,
    )

    backend = _FakeBackend()
    sup = Supervisor(backend=backend, publisher=_FakePublisher())
    await sup.spawn_and_publish(str(agent["id"]))

    got = await db.get_agent(agent["id"])
    assert got["status"] == "live"
    assert len(backend.spawned) == 1
    await db.close_db()
