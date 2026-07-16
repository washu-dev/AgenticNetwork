"""Tests for the cluster build + host-loop spawn pipeline (Phase 6 + Note #8).

Exercised with injected fakes — no real GitHub, Docker Hub, ADS, or srun.
"""

from __future__ import annotations

import pytest

from registration_service import db, supervisor as sup_mod
from registration_service.backend import ClusterBackend
from registration_service.fake_db import reset_fake_db
from registration_service.supervisor import Supervisor, _image_exists_on_dockerhub

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


async def _make_custom(approved=True):
    return await db.create_agent(
        name="Paper Reviewer",
        description="Reviews papers",
        handler_type="custom",
        # No "key" on purpose — mirrors what the DB stored before the key-capture fix;
        # exercises the supervisor's name-fallback in _build_agent_card / routing.
        oasf_skills=[{"name": "nlp/text-summarization", "id": 1}],
        config={"class_name": "H", "python_version": "3.12", "requirements": [], "extra": {}},
        approved=approved,
    )


def _cluster_env(monkeypatch):
    monkeypatch.setenv("SPAWN_BACKEND", "cluster")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPO", "washu-dev/agent-directory")


# ── Approval → commit → building ─────────────────────────────────────────────────

async def test_cluster_approve_commits_and_defers(monkeypatch):
    _cluster_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(approved=True)

    sup = Supervisor(backend=ClusterBackend(), publisher=_FakePublisher())
    committed = {}

    async def fake_commit(a, skills):
        committed["name"] = a["name"]

    monkeypatch.setattr(sup, "_commit_to_github", fake_commit)
    await sup.spawn_and_publish(str(agent["id"]))

    got = await db.get_agent(agent["id"])
    assert got["status"] == "building"
    assert committed.get("name") == "Paper Reviewer"
    await db.close_db()


async def test_cluster_rejects_non_custom_handler(monkeypatch):
    """A non-custom agent on the cluster backend must NOT go ghost-live.

    ClusterBackend has no spawn path for llm/api_proxy, so spawn_and_publish must
    fail loudly and mark the agent 'failed' rather than publishing it to ADS and
    marking it 'live' with no process behind it.
    """
    _cluster_env(monkeypatch)
    await _fresh_db()
    agent = await db.create_agent(
        name="Chatty",
        description="An LLM agent",
        handler_type="llm",
        oasf_skills=[{"name": "nlp/text-summarization", "id": 1}],
        config={"system_prompt": "You are helpful."},
        approved=True,
    )

    sup = Supervisor(backend=ClusterBackend(), publisher=_FakePublisher())
    with pytest.raises(RuntimeError, match="not supported on the cluster"):
        await sup.spawn_and_publish(str(agent["id"]))

    got = await db.get_agent(agent["id"])
    assert got["status"] == "failed"
    await db.close_db()


# ── Build poll → image_ready (NOT live; host loop does the spawn) ─────────────────

async def test_build_poll_marks_image_ready(monkeypatch):
    _cluster_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(approved=True)
    await db.update_agent(agent["id"], status="building")

    async def yes(image, since=None):
        return True

    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", yes)
    sup = Supervisor(backend=ClusterBackend(), publisher=_FakePublisher())
    await sup._check_building_agents()

    got = await db.get_agent(agent["id"])
    assert got["status"] == "image_ready"   # waits for the host loop, not live yet
    await db.close_db()


# ── Host loop: spawn-queue → finalize → live, then stop queues the PID ────────────

async def test_spawn_queue_finalize_and_stop(monkeypatch):
    _cluster_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(approved=True)
    aid = str(agent["id"])
    await db.update_agent(agent["id"], status="image_ready")

    backend = ClusterBackend()
    sup = Supervisor(backend=backend, publisher=_FakePublisher())

    # spawn-queue surfaces the image_ready agent with a runnable srun argv + env.
    queue = await sup.spawn_queue()
    assert len(queue) == 1
    entry = queue[0]
    assert entry["agent_id"] == aid
    assert entry["argv"][0] == "srun"
    assert "agent_skeleton.container_runner" in " ".join(entry["argv"])
    assert "AGENT_CONFIG_JSON" in entry["env"]
    assert f"agenticnetwork-custom-paper-reviewer" in " ".join(entry["argv"])

    # finalize → live + PID recorded.
    await sup.finalize_spawned(aid, pid=4242)
    got = await db.get_agent(agent["id"])
    assert got["status"] == "live"
    assert got["endpoint_url"]

    # stop → the PID is queued for the host loop to kill.
    await backend.stop(aid)
    assert sup.stop_queue() == [4242]
    assert sup.stop_queue() == []   # consumed once
    await db.close_db()


# ── Local path unaffected (no SPAWN_BACKEND → direct spawn) ───────────────────────

async def test_delete_github_context_gated_and_invoked(monkeypatch):
    await _fresh_db()
    sup = Supervisor(backend=ClusterBackend(), publisher=_FakePublisher())
    called = []

    async def fake_delete(slug):
        called.append(slug)

    monkeypatch.setattr(sup_mod, "_delete_from_github", fake_delete)

    # Not cluster-enabled → no-op.
    monkeypatch.delenv("SPAWN_BACKEND", raising=False)
    await sup.delete_github_context("Paper Reviewer")
    assert called == []

    # Cluster-enabled → deletes using the image slug.
    _cluster_env(monkeypatch)
    await sup.delete_github_context("Paper Reviewer")
    assert called == ["paper-reviewer"]
    await db.close_db()


async def test_local_approve_spawns_directly(monkeypatch):
    monkeypatch.delenv("SPAWN_BACKEND", raising=False)
    await _fresh_db()
    agent = await _make_custom(approved=True)

    sup = Supervisor(backend=_FakeBackend(), publisher=_FakePublisher())
    await sup.spawn_and_publish(str(agent["id"]))

    got = await db.get_agent(agent["id"])
    assert got["status"] == "live"
    assert len(sup._backend.spawned) == 1
    await db.close_db()


# ── Docker Hub poll helper ────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kw):
        return self._resp


async def test_image_exists_true(monkeypatch):
    monkeypatch.setattr(
        sup_mod.httpx, "AsyncClient",
        lambda *a, **k: _FakeClient(_Resp(200, {"last_updated": "2099-01-01T00:00:00Z"})),
    )
    assert await _image_exists_on_dockerhub("di2washu/agenticnetwork-custom-x") is True


async def test_image_exists_false_on_404(monkeypatch):
    monkeypatch.setattr(
        sup_mod.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_Resp(404)),
    )
    assert await _image_exists_on_dockerhub("di2washu/agenticnetwork-custom-x") is False


# ── Recovery on redeploy: a live custom agent is re-queued, not lost ──────────────

async def test_recover_live_agent_requeues_not_stops(monkeypatch):
    """Regression: recover_live_agents must NOT pass row['updated_at'] as the
    Docker Hub freshness bound. `updated_at` is bumped on go-live, which is always
    *after* the image was built, so a `since` check would always fail and wrongly
    mark the agent 'stopped'. With the fix, a live custom agent whose image still
    exists is re-queued to 'image_ready' for the host spawn loop.
    """
    _cluster_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(approved=True)
    # Simulate a prior deploy that took the agent all the way live (bumps updated_at
    # to "now", i.e. after any image build time).
    await db.update_agent(agent["id"], status="live", port=19200)

    seen_since = []

    async def exists(image, since=None):
        # Mirror the real contract: if a `since` bound is (wrongly) passed, an image
        # built before go-live would be rejected — which is exactly the bug.
        seen_since.append(since)
        return since is None

    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", exists)
    sup = Supervisor(backend=ClusterBackend(), publisher=_FakePublisher())
    await sup.recover_live_agents()

    got = await db.get_agent(agent["id"])
    assert got["status"] == "image_ready", "live custom agent should be re-queued"
    assert seen_since == [None], "recovery must not gate on updated_at freshness"

    # And it surfaces to the host spawn loop.
    queue = await sup.spawn_queue()
    assert [e["agent_id"] for e in queue] == [str(agent["id"])]
    await db.close_db()
