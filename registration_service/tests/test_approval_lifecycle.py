"""Approval and teardown lifecycle of uploaded-code agents, through the API.

The real Supervisor runs on the in-memory store with a stub backend and a stub
directory (no processes, no dirctl), so each test can check what was started,
stopped, published and unpublished:

- an upload is validated in a staging directory: a rejected one leaves the
  approved files and the row untouched; an accepted one replaces the tree by
  rename and returns the agent to the pre-approval state;
- an upload, approval, rejection, restart and delete of one agent run one at a
  time, whichever spelling of the agent's UUID the request uses, and the
  supervisor's launches (health restart, build poll, deploy, host-loop report)
  hold the same lock;
- revoking approval (reject, or an accepted re-upload) stops the agent, removes
  its card and drops its queued or in-flight build, spawn and deploy work;
- on a build backend (cluster, Cloud Run) uploaded code runs only from the
  image built for its approval, and every approval triggers a new build;
- a card the directory did not confirm removing keeps its CID on the row until
  a later attempt succeeds.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import logging
import threading
import uuid
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentnet.directory.publisher import DirectoryUnavailable
from registration_service import db, server
from registration_service import supervisor as sup_mod
from registration_service.archive import list_tree
from registration_service.backend import (
    ClusterBackend,
    CloudRunBackend,
    _cloudrun_service_id,
    _CloudRunClient,
    _image_slug,
)
from registration_service.fake_db import init_fake_db, reset_fake_db
from registration_service.server import app
from registration_service.supervisor import Supervisor
from registration_service.tests.conftest import (
    CUSTOM_PAYLOAD,
    INVALID_HANDLER_SRC,
    LLM_PAYLOAD,
    VALID_HANDLER_SRC,
)

pytestmark = pytest.mark.asyncio

V2_HANDLER_SRC = VALID_HANDLER_SRC.replace('"ok"', '"v2"')

# A valid multi-file project: the entry file imports a sibling package.
V1_TREE = {
    "handler.py": (
        "from agent_skeleton import AgentHandler\n"
        "from mylib import tag\n"
        "class MyAgentHandler(AgentHandler):\n"
        "    async def handle_structured(self, u, files=[]):\n"
        "        return {'answer': tag(u)}\n"
    ),
    "mylib/__init__.py": "def tag(s): return 'tagged:' + s\n",
}


def _py(src: str) -> dict:
    return {"file": ("handler.py", src.encode("utf-8"), "text/x-python")}


def _zip(files: dict[str, str]) -> dict:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return {"file": ("project.zip", buf.getvalue(), "application/zip")}


class _Backend:
    """Records lifecycle calls; starts nothing. ``spawn`` returns ``url`` (what
    an off-host backend returns as the agent's address)."""

    owns_build = False

    def __init__(self, url: str | None = None) -> None:
        self.url = url
        self.spawned: list[str] = []
        self.stopped: list[str] = []
        self.alive: set[str] = set()

    async def spawn(self, *, agent_id, **_):
        self.spawned.append(agent_id)
        self.alive.add(agent_id)
        return self.url

    async def stop(self, agent_id):
        self.stopped.append(agent_id)
        self.alive.discard(agent_id)

    async def is_alive(self, agent_id):
        return agent_id in self.alive


class _Publisher:
    """Records directory calls in order; ``up=False`` makes them fail the way
    DirectoryPublisher does (publish and healthcheck raise, unpublish returns
    False)."""

    def __init__(self) -> None:
        self.up = True
        self.calls: list[tuple] = []
        self._n = 0

    def publish(self, card, advertise_url=None):
        if not self.up:
            raise DirectoryUnavailable("directory down")
        self._n += 1
        cid = f"cid-{self._n}"
        self.calls.append(("publish", cid, advertise_url))
        return SimpleNamespace(backend_ref=cid)

    def unpublish(self, cid):
        self.calls.append(("unpublish", cid))
        return self.up

    def healthcheck(self, timeout=3.0):
        self.calls.append(("healthcheck",))
        if not self.up:
            raise DirectoryUnavailable("directory down")


@pytest_asyncio.fixture
async def api(monkeypatch, tmp_path):
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    monkeypatch.delenv("SPAWN_BACKEND", raising=False)
    reset_fake_db()
    db._pool = None
    await db.init_db()
    backend, publisher = _Backend(), _Publisher()
    sup = Supervisor(backend=backend, publisher=publisher)
    monkeypatch.setattr(sup_mod, "_supervisor", sup)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            client=client, sup=sup, backend=backend, publisher=publisher, root=tmp_path
        )
    await db.close_db()
    reset_fake_db()
    db._pool = None


async def _uploaded(api, files=None) -> str:
    resp = await api.client.post("/agents", json=CUSTOM_PAYLOAD)
    assert resp.status_code == 201, resp.text
    aid = resp.json()["id"]
    resp = await api.client.post(f"/agents/{aid}/handler", files=files or _py(VALID_HANDLER_SRC))
    assert resp.status_code == 200, resp.text
    return aid


async def _live(api, files=None) -> str:
    aid = await _uploaded(api, files)
    resp = await api.client.post(f"/agents/{aid}/approve")
    assert resp.status_code == 200, resp.text
    row = await db.get_agent(aid)
    assert row["status"] == "live" and row["approved"] is True and row["ads_cid"]
    return aid


def _tree(root) -> dict[str, str]:
    return {rel: (root / rel).read_text() for rel in list_tree(root)}


def _cluster_env(monkeypatch, backend_name: str = "cluster") -> None:
    monkeypatch.setenv("SPAWN_BACKEND", backend_name)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPO", "example-org/agents")


def _cluster_supervisor(api, monkeypatch) -> Supervisor:
    """A real Supervisor on a ClusterBackend (no GitHub calls), serving the API."""
    _cluster_env(monkeypatch)
    sup = Supervisor(backend=ClusterBackend(), publisher=api.publisher)
    sup._commit_to_github = AsyncMock()
    sup.delete_github_context = AsyncMock()
    monkeypatch.setattr(sup_mod, "_supervisor", sup)
    return sup


def _blocking_publish(publisher) -> tuple[threading.Event, threading.Event]:
    """Make publish wait in its worker thread (as dirctl does) until released."""
    started, release = threading.Event(), threading.Event()
    publish = publisher.publish

    def blocked(card, advertise_url=None):
        started.set()
        release.wait(5)
        return publish(card, advertise_url=advertise_url)

    publisher.publish = blocked
    return started, release


async def _until(event: threading.Event) -> None:
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("not reached")


# ── Upload: validate in staging, swap by rename ───────────────────────────────────


async def test_rejected_py_reupload_keeps_the_approved_file_and_row(api) -> None:
    aid = await _live(api)
    before = await db.get_agent(aid)

    resp = await api.client.post(f"/agents/{aid}/handler", files=_py(INVALID_HANDLER_SRC))

    assert resp.status_code == 422
    assert await db.get_agent(aid) == before
    assert _tree(api.root / aid) == {"handler.py": VALID_HANDLER_SRC}
    assert [p.name for p in api.root.iterdir()] == [aid]  # no staging dir left
    assert api.backend.stopped == []


@pytest.mark.parametrize(
    "bad_tree",
    [
        pytest.param({"handler.py": INVALID_HANDLER_SRC, "extra.py": "x = 1\n"}, id="invalid-entry"),
        pytest.param({"other.py": "x = 1\n"}, id="missing-entry"),
    ],
)
async def test_rejected_zip_reupload_keeps_the_approved_tree_and_row(api, bad_tree) -> None:
    aid = await _live(api, _zip(V1_TREE))
    before = await db.get_agent(aid)

    resp = await api.client.post(f"/agents/{aid}/handler", files=_zip(bad_tree))

    assert resp.status_code == 422
    assert await db.get_agent(aid) == before
    assert _tree(api.root / aid) == V1_TREE
    assert [p.name for p in api.root.iterdir()] == [aid]
    assert api.backend.stopped == []


async def test_accepted_reupload_replaces_the_tree_and_revokes_approval(api) -> None:
    aid = await _live(api, _zip(V1_TREE))
    cid = (await db.get_agent(aid))["ads_cid"]

    resp = await api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))

    assert resp.status_code == 200, resp.text
    # Replaced, not merged: the old project's sibling package is gone.
    assert _tree(api.root / aid) == {"handler.py": V2_HANDLER_SRC}
    assert [p.name for p in api.root.iterdir()] == [aid]
    row = await db.get_agent(aid)
    assert row["approved"] is False and row["status"] == "registered"
    assert row["is_archive"] is False
    assert row["handler_file_path"] == str(api.root / aid / "handler.py")
    assert row["ads_cid"] is None and row["image_ref"] is None
    assert api.backend.stopped == [aid]
    assert ("unpublish", cid) in api.publisher.calls


async def test_zip_upload_leaves_out_bytecode_and_says_so(api) -> None:
    """A zip of a project that was run locally carries __pycache__/: the upload is
    accepted, those files are not stored, and the response says so."""
    resp = await api.client.post("/agents", json=CUSTOM_PAYLOAD)
    aid = resp.json()["id"]
    files = {**V1_TREE, "mylib/__pycache__/__init__.cpython-311.pyc": "bytecode"}

    resp = await api.client.post(f"/agents/{aid}/handler", files=_zip(files))

    assert resp.status_code == 200, resp.text
    assert any("Skipped 1 file(s)" in w for w in resp.json()["warnings"])
    assert _tree(api.root / aid) == V1_TREE
    assert not (api.root / aid / "mylib" / "__pycache__").exists()


async def test_uploads_for_one_agent_do_not_interleave(api) -> None:
    aid = await _live(api)
    lock = server._agent_lock(aid)
    await lock.acquire()  # an upload, approval or rejection of this agent in progress
    try:
        pending = asyncio.create_task(
            api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))
        )
        await asyncio.sleep(0.1)
        assert not pending.done()
        assert _tree(api.root / aid) == {"handler.py": VALID_HANDLER_SRC}
        assert (await db.get_agent(aid))["approved"] is True
    finally:
        lock.release()
    resp = await pending
    assert resp.status_code == 200, resp.text
    assert _tree(api.root / aid) == {"handler.py": V2_HANDLER_SRC}
    assert (await db.get_agent(aid))["approved"] is False


@pytest.mark.parametrize(
    "spell",
    [str.upper, lambda s: s.replace("-", ""), lambda s: f"urn:uuid:{s}"],
    ids=["uppercase", "hex", "urn"],
)
async def test_any_spelling_of_the_id_names_the_same_agent(api, spell) -> None:
    aid = await _live(api)
    lock = server._agent_lock(aid)
    await lock.acquire()
    try:
        pending = asyncio.create_task(
            api.client.post(f"/agents/{spell(aid)}/handler", files=_py(V2_HANDLER_SRC))
        )
        await asyncio.sleep(0.1)
        assert not pending.done()  # the same lock as the canonical id
    finally:
        lock.release()
    resp = await pending
    assert resp.status_code == 200, resp.text
    assert [p.name for p in api.root.iterdir()] == [aid]
    assert _tree(api.root / aid) == {"handler.py": V2_HANDLER_SRC}
    assert (await db.get_agent(aid))["handler_file_path"] == str(api.root / aid / "handler.py")
    assert api.backend.stopped == [aid]


async def test_upload_waits_for_an_inflight_restart(api) -> None:
    """A restart's new process starts from the approved files; the upload then
    stops it and returns the agent to the pre-approval state."""
    aid = await _live(api)
    spawning, release = asyncio.Event(), asyncio.Event()
    started_from: list[str] = []
    spawn = api.backend.spawn

    async def slow_spawn(*, agent_id, **kwargs):
        spawning.set()
        await release.wait()
        started_from.append((api.root / agent_id / "handler.py").read_text())
        return await spawn(agent_id=agent_id, **kwargs)

    api.backend.spawn = slow_spawn
    restart = asyncio.create_task(api.client.post(f"/agents/{aid}/restart"))
    await asyncio.wait_for(spawning.wait(), 5)  # the restart holds the agent lock
    upload = asyncio.create_task(
        api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))
    )
    await asyncio.sleep(0.1)
    assert not upload.done()
    release.set()

    assert (await restart).status_code == 200
    assert (await upload).status_code == 200
    assert started_from == [VALID_HANDLER_SRC]
    row = await db.get_agent(aid)
    assert row["approved"] is False and row["status"] == "registered"
    assert row["ads_cid"] is None and api.backend.alive == set()


async def test_delete_waits_for_the_agent_lock(api) -> None:
    aid = await _live(api)
    lock = server._agent_lock(aid)
    await lock.acquire()  # an upload of this agent in progress
    try:
        pending = asyncio.create_task(api.client.delete(f"/agents/{aid}"))
        await asyncio.sleep(0.1)
        assert not pending.done()
        assert await db.get_agent(aid) is not None
    finally:
        lock.release()
    assert (await pending).status_code == 204
    assert await db.get_agent(aid) is None
    assert list(api.root.iterdir()) == []


async def test_source_review_waits_for_the_agent_lock(api) -> None:
    # The source, manifest and revision then describe one upload.
    aid = await _uploaded(api)
    lock = server._agent_lock(aid)
    await lock.acquire()  # an upload of this agent in progress
    try:
        pending = asyncio.create_task(api.client.get(f"/agents/{aid}/handler"))
        await asyncio.sleep(0.1)
        assert not pending.done()
    finally:
        lock.release()
    assert (await pending).status_code == 200


# ── Reject: revoking approval stops the agent ─────────────────────────────────────


async def test_reject_live_agent_stops_it_and_removes_its_card(api, monkeypatch) -> None:
    aid = await _live(api)
    cid = (await db.get_agent(aid))["ads_cid"]
    # A crash the health loop is about to restart (still in backoff).
    api.backend.alive.clear()
    monkeypatch.setattr(sup_mod, "_BACKOFF_BASE_SECONDS", 60.0)
    await api.sup._check_all_live_agents()
    restart = api.sup._restart_tasks[aid]

    resp = await api.client.post(f"/agents/{aid}/reject", json={"reason": "revoked"})

    assert resp.status_code == 200, resp.text
    row = await db.get_agent(aid)
    assert row["approved"] is False and row["status"] == "registered"
    assert row["ads_cid"] is None
    assert api.backend.stopped == [aid]
    assert ("unpublish", cid) in api.publisher.calls
    assert restart.cancelled() and api.sup._restart_tasks == {}
    assert api.backend.spawned == [aid]  # only the approved spawn


async def test_reject_registered_agent_stops_nothing(api) -> None:
    aid = await _uploaded(api)

    resp = await api.client.post(f"/agents/{aid}/reject", json={"reason": "not yet"})

    assert resp.status_code == 200, resp.text
    row = await db.get_agent(aid)
    assert row["approved"] is False and row["status"] == "registered"
    assert api.backend.stopped == [] and api.publisher.calls == []


async def test_spawn_reported_after_reject_is_stopped_not_published(api, monkeypatch) -> None:
    """The cluster host loop may already have taken the agent off the spawn queue
    when approval is revoked; the process it then reports is stopped, unpublished."""
    _cluster_env(monkeypatch)
    backend = ClusterBackend()
    sup = Supervisor(backend=backend, publisher=api.publisher)
    monkeypatch.setattr(sup_mod, "_supervisor", sup)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v1")
    assert [e["agent_id"] for e in await sup.spawn_queue()] == [aid]

    resp = await api.client.post(f"/agents/{aid}/reject", json={"reason": "revoked"})
    assert resp.status_code == 200, resp.text
    assert await sup.spawn_queue() == []

    await sup.finalize_spawned(aid, pid=4242)

    row = await db.get_agent(aid)
    assert row["status"] == "registered" and row["ads_cid"] is None
    assert not any(c[0] == "publish" for c in api.publisher.calls)
    assert sup.stop_queue() == [4242]


async def test_unapproved_launch_is_stopped_and_not_queued_again(api, monkeypatch) -> None:
    """A launch reported for a row without approval is stopped and publishes
    nothing, and the host loop is not handed the row again."""
    _cluster_env(monkeypatch)
    sup = Supervisor(backend=ClusterBackend(), publisher=api.publisher)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=False, status="image_ready", image_ref="sha256:v1")
    assert await sup.spawn_queue() == []

    await sup.finalize_spawned(aid, pid=4242)

    assert await sup.spawn_queue() == []
    assert sup.stop_queue() == [4242]
    assert api.publisher.calls == []


async def test_spawn_queue_leaves_out_a_row_whose_reject_is_in_progress(api, monkeypatch) -> None:
    """A reject revokes approval before it removes the card and moves the row
    back to 'registered'; meanwhile the host loop is not handed the row."""
    sup = _cluster_supervisor(api, monkeypatch)
    aid = await _uploaded(api)
    # The row still records a card whose earlier removal was not confirmed.
    await db.update_agent(
        aid, approved=True, status="image_ready", image_ref="sha256:v1", ads_cid="cid-old"
    )
    started, release = threading.Event(), threading.Event()
    unpublish = api.publisher.unpublish

    def slow_unpublish(cid):
        started.set()
        release.wait(5)
        return unpublish(cid)

    api.publisher.unpublish = slow_unpublish
    reject = asyncio.create_task(api.client.post(f"/agents/{aid}/reject", json={"reason": "x"}))
    await _until(started)
    queued = await sup.spawn_queue()
    release.set()
    assert (await reject).status_code == 200

    assert queued == []
    assert (await db.get_agent(aid))["status"] == "registered"


async def test_cluster_startup_returns_an_unapproved_live_row_to_approval(api, monkeypatch) -> None:
    """A row left 'live' without approval (code uploaded after its approval by an
    earlier version, which kept the status) is not relaunched from its old image."""
    _cluster_env(monkeypatch)
    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", AsyncMock(return_value=True))
    sup = Supervisor(backend=ClusterBackend(), publisher=api.publisher)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=False, status="live", image_ref="sha256:v1", ads_cid="cid-1")

    await sup.recover_live_agents()

    row = await db.get_agent(aid)
    assert row["status"] == "registered" and row["approved"] is False
    assert row["image_ref"] is None and row["ads_cid"] is None
    assert await sup.spawn_queue() == []


@pytest.mark.parametrize("backend_kind", ["subprocess", "cloudrun"])
@pytest.mark.parametrize("status", ["live", "image_ready", "registered"])
async def test_startup_stops_what_runs_for_a_row_that_lost_its_approval(
    api, backend_kind, status
) -> None:
    """A row without approval that may still run (left 'live' or 'image_ready' by
    an earlier version's upload, or 'registered' with its image by its reject) is
    stopped, unpublished and returned to approval: not kept live, relaunched or
    deployed."""
    api.backend.publishes_decoupled = backend_kind == "cloudrun"
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=False, status=status, image_ref="sha256:v1", ads_cid="cid-1")
    api.backend.alive.add(aid)

    await api.sup.recover_live_agents()

    row = await db.get_agent(aid)
    assert row["status"] == "registered" and row["approved"] is False
    assert row["image_ref"] is None and row["ads_cid"] is None
    assert api.backend.stopped == [aid] and api.backend.spawned == []
    assert ("unpublish", "cid-1") in api.publisher.calls


@pytest.mark.parametrize("status", ["live", "image_ready"])
async def test_startup_leaves_a_row_whose_stop_fails_for_a_later_retry(api, status) -> None:
    """Startup still completes: the card is removed, the service is neither
    confirmed live nor deployed again, and the row keeps its status, so the next
    startup or an admin's reject stops it."""
    api.backend.publishes_decoupled = True
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=False, status=status, image_ref="sha256:v1", ads_cid="cid-1")
    api.backend.alive.add(aid)

    async def failing_stop(agent_id):
        raise RuntimeError("delete failed")

    api.backend.stop = failing_stop

    await api.sup.recover_live_agents()

    row = await db.get_agent(aid)
    assert row["status"] == status and row["approved"] is False and row["ads_cid"] is None
    assert api.backend.spawned == []


@pytest.mark.parametrize(
    "status,image_ref",
    [("registered", "sha256:v1"), ("image_ready", "sha256:v1"), ("building", None)],
)
async def test_reject_retries_the_stop_a_startup_could_not_make(api, status, image_ref) -> None:
    """A row the startup reset could not stop keeps its status and image, the
    marker the next startup retries by. An admin's reject of it (the row is
    pending approval) stops the agent before it clears that marker."""
    api.backend.publishes_decoupled = True
    api.backend.owns_build = True
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=False, status=status, image_ref=image_ref)
    api.backend.alive.add(aid)
    stop = api.backend.stop

    async def failing_stop(agent_id):
        raise RuntimeError("delete failed")

    api.backend.stop = failing_stop
    await api.sup.recover_live_agents()
    assert (await db.get_agent(aid))["status"] == status
    api.backend.stop = stop

    resp = await api.client.post(f"/agents/{aid}/reject", json={"reason": "again"})

    assert resp.status_code == 200, resp.text
    row = await db.get_agent(aid)
    assert row["status"] == "registered" and row["image_ref"] is None
    assert api.backend.stopped == [aid] and aid not in api.backend.alive


async def test_approve_retries_the_stop_a_startup_could_not_make(api, monkeypatch) -> None:
    """The same for an approval of a 'registered' row that kept its image: the
    approval stops the agent and clears the image before it starts the build."""
    _cluster_env(monkeypatch, "cloudrun")
    api.backend.publishes_decoupled = True
    api.backend.owns_build = True
    api.sup._commit_to_github = AsyncMock()
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=False, status="registered", image_ref="sha256:v1")
    api.backend.alive.add(aid)
    stop = api.backend.stop

    async def failing_stop(agent_id):
        raise RuntimeError("delete failed")

    api.backend.stop = failing_stop
    await api.sup.recover_live_agents()
    assert (await db.get_agent(aid))["image_ref"] == "sha256:v1"
    api.backend.stop = stop

    resp = await api.client.post(f"/agents/{aid}/approve")

    assert resp.status_code == 200, resp.text
    row = await db.get_agent(aid)
    assert row["status"] == "building" and row["approved"] is True and row["image_ref"] is None
    assert api.backend.stopped == [aid] and aid not in api.backend.alive


@pytest.mark.parametrize(
    "first,second", [("weather_bot", "Weather Bot"), ("x" * 60 + "a", "x" * 60 + "b")]
)
async def test_names_that_share_a_cloud_run_service_are_refused(api, first, second) -> None:
    """Every Cloud Run deploy, stop and health check names an agent's service by
    its id (the slug sanitized further and cut at 63 characters). Two names with
    one service id would stop and replace each other's service, so the second
    is refused like a name with the same slug."""
    assert _image_slug(first) != _image_slug(second)
    assert _cloudrun_service_id(first) == _cloudrun_service_id(second)
    resp = await api.client.post("/agents", json={**CUSTOM_PAYLOAD, "name": first})
    assert resp.status_code == 201, resp.text

    resp = await api.client.post("/agents", json={**CUSTOM_PAYLOAD, "name": second})

    assert resp.status_code == 409, resp.text
    assert _cloudrun_service_id(second) in resp.json()["detail"]


async def test_build_that_finishes_during_a_reject_is_not_queued(api, monkeypatch) -> None:
    _cluster_env(monkeypatch)
    sup = Supervisor(backend=ClusterBackend(), publisher=api.publisher)
    monkeypatch.setattr(sup_mod, "_supervisor", sup)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="building")

    async def image_exists(image, since=None):
        # The reject lands while Docker Hub is being asked.
        resp = await api.client.post(f"/agents/{aid}/reject", json={"reason": "revoked"})
        assert resp.status_code == 200, resp.text
        return True

    async def digest(image, tag="latest"):
        return "sha256:v1"

    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", image_exists)
    monkeypatch.setattr(sup_mod, "_image_digest_on_dockerhub", digest)

    await sup._check_building_agents()

    row = await db.get_agent(aid)
    assert row["status"] == "registered" and row["image_ref"] is None
    assert await sup.spawn_queue() == []


async def test_build_poll_does_not_advance_a_row_approved_again_meanwhile(api, monkeypatch) -> None:
    """A reject and a new approval land while Docker Hub answers: the image found
    was checked against the earlier approval's time, so the row keeps waiting for
    the build of the new one."""
    _cluster_env(monkeypatch)
    api.backend.owns_build = True
    api.sup._commit_to_github = AsyncMock()
    aid = await _uploaded(api)
    assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200

    async def image_exists(image, since=None):
        resp = await api.client.post(f"/agents/{aid}/reject", json={"reason": "x"})
        assert resp.status_code == 200, resp.text
        await asyncio.sleep(0.001)  # the new approval gets a later updated_at
        assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200
        return True

    async def digest(image, tag="latest"):
        return "sha256:previous"

    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", image_exists)
    monkeypatch.setattr(sup_mod, "_image_digest_on_dockerhub", digest)
    await api.sup._check_building_agents()

    row = await db.get_agent(aid)
    assert row["status"] == "building" and row["approved"] is True
    assert row["image_ref"] is None


async def test_reject_during_the_build_polls_row_check_is_not_overwritten(api, monkeypatch) -> None:
    """The build poll re-reads the row, then writes 'image_ready'; a store round
    trip separates the two. A reject that lands there waits for the poll's lock."""
    _cluster_env(monkeypatch)
    sup = Supervisor(backend=ClusterBackend(), publisher=api.publisher)
    monkeypatch.setattr(sup_mod, "_supervisor", sup)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="building")
    rejects: list[asyncio.Task] = []

    async def image_exists(image, since=None):
        return True

    async def digest(image, tag="latest"):
        return "sha256:v1"

    real_get = sup_mod.get_agent

    async def slow_get(agent_id):
        row = await real_get(agent_id)
        if not rejects:
            rejects.append(asyncio.create_task(
                api.client.post(f"/agents/{aid}/reject", json={"reason": "x"})))
            await asyncio.sleep(0.1)  # a Postgres round trip
        return row

    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", image_exists)
    monkeypatch.setattr(sup_mod, "_image_digest_on_dockerhub", digest)
    monkeypatch.setattr(sup_mod, "get_agent", slow_get)
    await sup._check_building_agents()
    assert rejects, "the build poll wrote image_ready without re-reading the row"
    assert (await rejects[0]).status_code == 200

    row = await db.get_agent(aid)
    assert row["approved"] is False and row["status"] == "registered" and row["image_ref"] is None
    assert await sup.spawn_queue() == []


async def test_launch_reported_after_a_new_approval_is_discarded(api, monkeypatch) -> None:
    """The host loop took the old image off the queue; meanwhile the code was
    replaced and the new code approved. The late launch is stopped and the row
    keeps waiting for the new build."""
    _cluster_env(monkeypatch)
    sup = Supervisor(backend=ClusterBackend(), publisher=api.publisher)
    sup._commit_to_github = AsyncMock()
    monkeypatch.setattr(sup_mod, "_supervisor", sup)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v1")
    assert [e["agent_id"] for e in await sup.spawn_queue()] == [aid]

    resp = await api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))
    assert resp.status_code == 200, resp.text
    assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200
    assert (await db.get_agent(aid))["status"] == "building"

    await sup.finalize_spawned(aid, pid=4242)

    row = await db.get_agent(aid)
    assert row["status"] == "building" and row["approved"] is True
    assert row["ads_cid"] is None
    assert not any(c[0] == "publish" for c in api.publisher.calls)
    assert sup.stop_queue() == [4242]


async def test_upload_during_a_cloud_deploy_waits_then_stops_it(api, monkeypatch) -> None:
    """The Cloud Run counterpart: a deploy holds the agent lock until the agent is
    live, so an upload that arrives meanwhile then stops it and removes its card;
    approving the new code sends it through the build."""
    _cluster_env(monkeypatch, "cloudrun")
    api.backend.owns_build = True
    api.sup._commit_to_github = AsyncMock()
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v1")
    listed = await db.get_agent(aid)
    in_deploy, release = asyncio.Event(), asyncio.Event()
    spawn = api.backend.spawn

    async def deploy(**kwargs):
        in_deploy.set()
        await release.wait()
        return await spawn(**kwargs)

    api.backend.spawn = deploy
    deploying = asyncio.create_task(api.sup._deploy_ready_agent(listed))
    await asyncio.wait_for(in_deploy.wait(), 5)  # the deploy holds the agent lock
    upload = asyncio.create_task(
        api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))
    )
    await asyncio.sleep(0.1)
    assert not upload.done()
    release.set()
    await deploying
    assert (await upload).status_code == 200

    row = await db.get_agent(aid)
    assert row["approved"] is False and row["status"] == "registered"
    assert row["ads_cid"] is None and row["image_ref"] is None
    assert [c[:2] for c in api.publisher.calls] == [("publish", "cid-1"), ("unpublish", "cid-1")]
    assert api.backend.stopped == [aid] and api.backend.alive == set()
    assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200
    assert (await db.get_agent(aid))["status"] == "building"


@pytest.mark.parametrize("revoke", ["reject", "delete"])
async def test_cloud_deploy_of_a_revoked_launch_does_not_run(api, monkeypatch, revoke) -> None:
    """A reject or delete that lands between the build poll's move to
    'image_ready' and the deploy: nothing is deployed, and a rejected row stays
    in the pre-approval state (not 'failed', which the startup purge deletes)."""
    _cluster_env(monkeypatch, "cloudrun")
    api.backend.owns_build = True
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v1")
    listed = await db.get_agent(aid)
    if revoke == "reject":
        resp = await api.client.post(f"/agents/{aid}/reject", json={"reason": "revoked"})
    else:
        resp = await api.client.delete(f"/agents/{aid}")
    assert resp.status_code in (200, 204), resp.text

    await api.sup._deploy_ready_agent(listed)

    assert api.backend.spawned == [] and api.publisher.calls == []
    row = await db.get_agent(aid)
    if revoke == "reject":
        assert row["status"] == "registered" and row["approved"] is False
    else:
        assert row is None


async def test_a_failed_cloud_deploy_deletes_the_service_it_created(api, monkeypatch) -> None:
    """A deploy can fail after Cloud Run created the service (granting the
    invoker, or a revision that never becomes ready). The service is deleted, so
    the failed row leaves nothing running, even once the startup purge removes
    it (a failed row without an image digest)."""
    _cluster_env(monkeypatch, "cloudrun")
    api.backend.owns_build = True
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref=None)
    spawn = api.backend.spawn

    async def deploy_then_fail(**kwargs):
        await spawn(**kwargs)  # the service exists
        raise RuntimeError("set_invoker: permission denied")

    api.backend.spawn = deploy_then_fail
    await api.sup._deploy_ready_agent(await db.get_agent(aid))

    assert (await db.get_agent(aid))["status"] == "failed"
    assert api.backend.stopped == [aid] and api.backend.alive == set()
    assert api.publisher.calls == []


@pytest.mark.parametrize("revoke", ["reject", "reupload"])
async def test_revoking_a_failed_cloud_deploy_removes_its_service(api, monkeypatch, revoke) -> None:
    """A deploy failed after Cloud Run created the service, and deleting the
    service then failed too. Revoking the approval deletes it."""
    _cluster_env(monkeypatch, "cloudrun")
    api.backend.owns_build = True
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v1")
    spawn, stop = api.backend.spawn, api.backend.stop

    async def deploy_then_fail(**kwargs):
        await spawn(**kwargs)  # the service exists
        raise RuntimeError("set_invoker: permission denied")

    async def delete_fails(agent_id):
        raise RuntimeError("delete: unavailable")

    api.backend.spawn, api.backend.stop = deploy_then_fail, delete_fails
    await api.sup._deploy_ready_agent(await db.get_agent(aid))
    assert (await db.get_agent(aid))["status"] == "failed" and aid in api.backend.alive
    api.backend.stop = stop

    if revoke == "reject":
        resp = await api.client.post(f"/agents/{aid}/reject", json={"reason": "revoked"})
    else:
        resp = await api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))
    assert resp.status_code == 200, resp.text

    row = await db.get_agent(aid)
    assert row["approved"] is False and row["status"] == "registered"
    assert api.backend.stopped == [aid] and api.backend.alive == set()


async def test_cloud_deploy_of_a_superseded_launch_does_not_run(api, monkeypatch) -> None:
    """New code uploaded and approved between the move to 'image_ready' and the
    deploy (both waited for the agent lock): the previous image is not deployed,
    and the row keeps waiting for the new build."""
    _cluster_env(monkeypatch, "cloudrun")
    api.backend.owns_build = True
    api.sup._commit_to_github = AsyncMock()
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v1")
    listed = await db.get_agent(aid)
    resp = await api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))
    assert resp.status_code == 200, resp.text
    assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200

    await api.sup._deploy_ready_agent(listed)

    assert api.backend.spawned == [] and api.publisher.calls == []
    row = await db.get_agent(aid)
    assert row["status"] == "building" and row["approved"] is True


# ── Re-upload of a running agent: back through the build ─────────────────────────


@pytest.mark.parametrize("backend_name", ["cluster", "cloudrun"])
async def test_reupload_of_running_agent_rebuilds_on_next_approval(
    api, monkeypatch, backend_name
) -> None:
    _cluster_env(monkeypatch, backend_name)
    api.backend.owns_build = True
    api.sup._commit_to_github = AsyncMock()
    aid = await _uploaded(api)
    assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200
    assert api.sup._commit_to_github.await_count == 1
    assert (await db.get_agent(aid))["status"] == "building"
    # Built, deployed and published.
    await db.update_agent(aid, status="live", image_ref="sha256:v1", ads_cid="cid-v1")

    resp = await api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))
    assert resp.status_code == 200, resp.text
    row = await db.get_agent(aid)
    assert row["approved"] is False and row["status"] == "registered"
    assert row["image_ref"] is None and row["ads_cid"] is None
    assert api.backend.stopped == [aid]
    assert ("unpublish", "cid-v1") in api.publisher.calls

    resp = await api.client.post(f"/agents/{aid}/approve")
    assert resp.status_code == 200, resp.text
    assert api.sup._commit_to_github.await_count == 2
    assert (await db.get_agent(aid))["status"] == "building"
    assert api.backend.spawned == []


# ── Approval is bound to the reviewed code ───────────────────────────────────────

_CHANGED = "The uploaded code changed since it was reviewed; review it again."


async def test_approve_with_the_reviewed_revision_starts_the_agent(api) -> None:
    aid = await _uploaded(api, _zip(V1_TREE))
    revision = (await api.client.get(f"/agents/{aid}/handler")).json()["revision"]

    resp = await api.client.post(f"/agents/{aid}/approve", json={"revision": revision})

    assert resp.status_code == 200, resp.text
    row = await db.get_agent(aid)
    assert row["approved"] is True and row["status"] == "live"
    assert api.backend.spawned == [aid]


async def test_approve_of_code_uploaded_after_the_review_is_refused(api) -> None:
    aid = await _uploaded(api)
    revision = (await api.client.get(f"/agents/{aid}/handler")).json()["revision"]
    resp = await api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))
    assert resp.status_code == 200, resp.text

    resp = await api.client.post(f"/agents/{aid}/approve", json={"revision": revision})

    assert resp.status_code == 409
    assert resp.json()["detail"] == _CHANGED
    assert (await db.get_agent(aid))["approved"] is False
    assert api.backend.spawned == []

    # Reviewing the new code gives the revision that approves it.
    review = (await api.client.get(f"/agents/{aid}/handler")).json()
    assert review["source"] == V2_HANDLER_SRC and review["revision"] != revision
    resp = await api.client.post(f"/agents/{aid}/approve", json={"revision": review["revision"]})
    assert resp.status_code == 200, resp.text
    assert (await db.get_agent(aid))["approved"] is True


async def test_approve_compares_the_revision_after_a_pending_upload_lands(api) -> None:
    """An upload holding the agent lock when the approval arrives is compared
    against, not approved: the check runs once the approval has the lock."""
    aid = await _uploaded(api)
    revision = (await api.client.get(f"/agents/{aid}/handler")).json()["revision"]
    lock = server._agent_lock(aid)
    await lock.acquire()
    approve = asyncio.create_task(
        api.client.post(f"/agents/{aid}/approve", json={"revision": revision})
    )
    await asyncio.sleep(0.05)
    assert not approve.done()
    server._replace_dir(_staged(api.root, aid, V2_HANDLER_SRC), api.root / aid)
    lock.release()

    resp = await approve
    assert resp.status_code == 409
    assert (await db.get_agent(aid))["approved"] is False


async def test_approve_without_a_revision_approves_the_stored_code(api) -> None:
    aid = await _uploaded(api)

    resp = await api.client.post(f"/agents/{aid}/approve")

    assert resp.status_code == 200, resp.text
    assert (await db.get_agent(aid))["approved"] is True
    assert api.backend.spawned == [aid]


def _staged(root, aid: str, src: str):
    staged = root / f".upload-{aid}-test"
    staged.mkdir()
    (staged / "handler.py").write_text(src)
    return staged


# ── Card IDs survive a directory outage ───────────────────────────────────────────


async def test_stop_keeps_the_card_id_until_the_directory_confirms(api) -> None:
    aid = await _live(api)
    cid = (await db.get_agent(aid))["ads_cid"]
    api.publisher.up = False

    await api.sup.stop_and_unpublish(aid)

    row = await db.get_agent(aid)
    assert row["status"] == "stopped" and row["ads_cid"] == cid
    assert api.backend.stopped == [aid]

    # Startup recovery retries the removal and clears the CID once confirmed.
    api.publisher.up = True
    await api.sup.recover_live_agents()
    assert (await db.get_agent(aid))["ads_cid"] is None
    assert api.publisher.calls[-1] == ("unpublish", cid)


async def test_delete_with_directory_down_returns_503_and_keeps_the_row(api) -> None:
    aid = await _live(api)
    cid = (await db.get_agent(aid))["ads_cid"]
    api.publisher.up = False

    resp = await api.client.delete(f"/agents/{aid}")

    assert resp.status_code == 503
    assert "directory" in resp.json()["detail"].lower()
    row = await db.get_agent(aid)
    assert row is not None and row["status"] == "stopped" and row["ads_cid"] == cid
    assert (api.root / aid).exists()

    api.publisher.up = True
    resp = await api.client.delete(f"/agents/{aid}")
    assert resp.status_code == 204
    assert await db.get_agent(aid) is None
    assert not (api.root / aid).exists()
    assert api.publisher.calls[-1] == ("unpublish", cid)


async def test_forced_delete_removes_the_agent_and_logs_the_card_left_behind(
    api, caplog
) -> None:
    aid = await _live(api)
    cid = (await db.get_agent(aid))["ads_cid"]
    api.publisher.up = False

    resp = await api.client.delete(f"/agents/{aid}")
    assert resp.status_code == 503
    assert "force" in resp.json()["detail"]
    assert await db.get_agent(aid) is not None

    with caplog.at_level(logging.WARNING, logger="registration_service.server"):
        resp = await api.client.delete(f"/agents/{aid}", params={"force": "true"})

    assert resp.status_code == 204, resp.text
    assert await db.get_agent(aid) is None
    assert not (api.root / aid).exists()
    assert any(
        r.levelno == logging.WARNING and aid in r.getMessage() and cid in r.getMessage()
        for r in caplog.records
    )


async def test_forced_delete_is_admin_only(api, monkeypatch) -> None:
    aid = await _live(api)
    api.publisher.up = False
    monkeypatch.setattr(server, "fake_db_enabled", lambda: False)
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")

    resp = await api.client.delete(
        f"/agents/{aid}", params={"force": "true"}, headers={"mail": "user@example.com"}
    )
    assert resp.status_code == 403
    assert await db.get_agent(aid) is not None

    resp = await api.client.delete(
        f"/agents/{aid}", params={"force": "true"}, headers={"mail": "admin@example.com"}
    )
    assert resp.status_code == 204, resp.text
    assert await db.get_agent(aid) is None


async def test_a_card_stored_by_a_publish_that_timed_out_keeps_its_id(api) -> None:
    """The directory stored the card but did not show it in its routing index in
    time; it may route it later. The agent goes live with that card's CID, so a
    delete removes the card."""

    def stored_not_indexed(card, advertise_url=None):
        exc = DirectoryUnavailable("record did not appear in routing index")
        exc.cid = "cid-stored"
        raise exc

    api.publisher.publish = stored_not_indexed
    aid = await _uploaded(api)
    assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200
    row = await db.get_agent(aid)
    assert row["status"] == "live" and row["ads_cid"] == "cid-stored"

    assert (await api.client.delete(f"/agents/{aid}")).status_code == 204
    assert api.publisher.calls == [("unpublish", "cid-stored")]


async def test_card_id_is_cleared_once_removed_even_if_the_stop_fails(api) -> None:
    aid = await _live(api)

    async def failing_stop(agent_id):
        raise RuntimeError("backend unavailable")

    api.backend.stop = failing_stop
    with pytest.raises(RuntimeError):
        await api.sup.stop_and_unpublish(aid)
    assert (await db.get_agent(aid))["ads_cid"] is None


class _CloudRunServices:
    """The Cloud Run Admin API's delete: NotFound once the service is gone."""

    def __init__(self, names: set[str]) -> None:
        self.names = names

    async def delete_service(self, name):
        from google.api_core.exceptions import NotFound

        if name not in self.names:
            raise NotFound(name)
        self.names.discard(name)
        return SimpleNamespace(result=AsyncMock())


async def test_delete_retry_on_cloud_run_succeeds_once_the_service_is_gone(api, monkeypatch) -> None:
    pytest.importorskip("google.api_core.exceptions")
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="live", ads_cid="cid-1")
    client = _CloudRunClient.__new__(_CloudRunClient)
    client._parent = "projects/example/locations/us-central1"
    services = _CloudRunServices(
        {client._service_name(_cloudrun_service_id(CUSTOM_PAYLOAD["name"]))}
    )
    client._services = services
    sup = Supervisor(backend=CloudRunBackend(client=client), publisher=api.publisher)
    monkeypatch.setattr(sup_mod, "_supervisor", sup)
    api.publisher.up = False

    resp = await api.client.delete(f"/agents/{aid}")
    assert resp.status_code == 503
    assert services.names == set()  # the service was deleted; the card was not

    api.publisher.up = True
    resp = await api.client.delete(f"/agents/{aid}")
    assert resp.status_code == 204, resp.text
    assert await db.get_agent(aid) is None
    assert api.publisher.calls[-1] == ("unpublish", "cid-1")


async def test_cluster_startup_clears_card_ids_left_on_stopped_rows(api, monkeypatch) -> None:
    """Each cluster deploy starts the directory empty, so a card ID kept through
    an outage names nothing: it is cleared, not retried."""
    _cluster_env(monkeypatch)
    sup = Supervisor(backend=ClusterBackend(), publisher=api.publisher)
    aid = await _uploaded(api)
    await db.update_agent(aid, status="stopped", ads_cid="cid-previous-deploy")
    api.publisher.up = False

    await sup.recover_live_agents()

    assert (await db.get_agent(aid))["ads_cid"] is None
    assert api.publisher.calls == []


async def test_cloud_recovery_clears_a_removed_card_id_when_the_redeploy_cannot_publish(api) -> None:
    api.backend.publishes_decoupled = True
    aid = await _uploaded(api)
    # Live, but its service is gone (the stub backend reports it not alive).
    await db.update_agent(aid, approved=True, status="live", image_ref="sha256:v1", ads_cid="cid-old")

    def publish_down(card, advertise_url=None):
        raise DirectoryUnavailable("directory down")

    api.publisher.publish = publish_down
    await api.sup.recover_live_agents()

    row = await db.get_agent(aid)
    assert ("unpublish", "cid-old") in api.publisher.calls
    assert row["status"] == "live" and row["ads_cid"] is None


async def test_a_card_left_by_an_earlier_failure_is_removed_at_the_next_publish(api, monkeypatch) -> None:
    _cluster_env(monkeypatch)
    sup = Supervisor(backend=ClusterBackend(), publisher=api.publisher)
    aid = await _uploaded(api)
    # Approved again after a revoke whose card removal the directory did not confirm.
    await db.update_agent(
        aid, approved=True, status="image_ready", image_ref="sha256:v2", ads_cid="cid-old"
    )

    await sup.finalize_spawned(aid, pid=4242)

    row = await db.get_agent(aid)
    assert row["status"] == "live" and row["ads_cid"] == "cid-1"
    assert api.publisher.calls[-1] == ("unpublish", "cid-old")


async def test_startup_purge_keeps_rows_that_still_hold_a_card_id(api) -> None:
    async def row(name: str, **fields) -> str:
        resp = await api.client.post("/agents", json={**CUSTOM_PAYLOAD, "name": name})
        assert resp.status_code == 201, resp.text
        await db.update_agent(resp.json()["id"], **fields)
        return resp.json()["id"]

    stopped_with_card = await row("Stopped With Card", status="stopped", ads_cid="cid-a")
    stopped = await row("Stopped", status="stopped")
    failed_with_card = await row("Failed With Card", status="failed", ads_cid="cid-b")
    failed = await row("Failed", status="failed")

    await init_fake_db()  # the startup purge

    assert await db.get_agent(stopped_with_card) is not None
    assert await db.get_agent(failed_with_card) is not None
    assert await db.get_agent(stopped) is None
    assert await db.get_agent(failed) is None
    # The postgres purge uses the same rule.
    assert "ads_cid IS NULL" in db._PURGEABLE_SQL


# ── The advertised endpoint is the one the backend reports ────────────────────────


@pytest.mark.parametrize(
    "returned,advertised",
    [
        ("https://agent-test-custom-agent-abc123-uc.a.run.app", "https://agent-test-custom-agent-abc123-uc.a.run.app"),
        (None, "http://localhost:{port}/"),
    ],
)
async def test_spawn_publishes_the_endpoint_the_backend_returns(api, returned, advertised) -> None:
    api.backend.url = returned
    aid = await _live(api)

    row = await db.get_agent(aid)
    expected = advertised.format(port=row["port"])
    assert row["endpoint_url"] == expected
    assert api.publisher.calls[-1][2] == expected


# ── Launches hold the agent lock ──────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["reject", "reupload", "delete"])
async def test_change_during_a_launch_publish_waits_then_stops_the_agent(
    api, monkeypatch, action
) -> None:
    """The host loop's report holds the agent lock through the publish: a reject,
    upload or delete that arrives meanwhile waits, then stops the agent and
    removes the card just published."""
    sup = _cluster_supervisor(api, monkeypatch)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v1")
    started, release = _blocking_publish(api.publisher)

    finalize = asyncio.create_task(sup.finalize_spawned(aid, pid=4242))
    await _until(started)
    if action == "reject":
        request = api.client.post(f"/agents/{aid}/reject", json={"reason": "revoked"})
    elif action == "reupload":
        request = api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))
    else:
        request = api.client.delete(f"/agents/{aid}")
    pending = asyncio.create_task(request)
    await asyncio.sleep(0.1)
    assert not pending.done()
    release.set()
    await finalize
    assert (await pending).status_code in (200, 204)

    assert sup.stop_queue() == [4242]
    assert [c[:2] for c in api.publisher.calls] == [("publish", "cid-1"), ("unpublish", "cid-1")]
    row = await db.get_agent(aid)
    if action == "delete":
        assert row is None
    else:
        assert row["status"] == "registered" and row["approved"] is False
        assert row["ads_cid"] is None


@pytest.mark.parametrize("action", ["delete", "restart"])
async def test_change_during_a_registration_launch_waits_for_it(api, action) -> None:
    """Registering an agent that uploads no code starts it at once, holding the
    agent lock: a delete or restart sent meanwhile waits, then removes the card
    the registration published."""
    started, release = _blocking_publish(api.publisher)
    register = asyncio.create_task(api.client.post("/agents", json=LLM_PAYLOAD))
    await _until(started)
    [listed] = (await api.client.get("/agents")).json()
    aid = listed["id"]
    if action == "delete":
        pending = asyncio.create_task(api.client.delete(f"/agents/{aid}"))
    else:
        pending = asyncio.create_task(api.client.post(f"/agents/{aid}/restart"))
    await asyncio.sleep(0.1)
    assert not pending.done()
    release.set()
    assert (await register).status_code == 201
    assert (await pending).status_code in (200, 204)

    published = [c[1] for c in api.publisher.calls if c[0] == "publish"]
    unpublished = [c[1] for c in api.publisher.calls if c[0] == "unpublish"]
    assert unpublished == ["cid-1"]
    row = await db.get_agent(aid)
    if action == "delete":
        assert published == ["cid-1"] and row is None
    else:
        assert published == ["cid-1", "cid-2"] and row["ads_cid"] == "cid-2"


async def test_launch_reported_for_a_deleted_agent_is_stopped(api, monkeypatch) -> None:
    """The host loop took the agent off the queue, then the agent was deleted:
    the process it reports is killed, and nothing is published."""
    sup = _cluster_supervisor(api, monkeypatch)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v1")
    assert [e["agent_id"] for e in await sup.spawn_queue()] == [aid]
    assert (await api.client.delete(f"/agents/{aid}")).status_code == 204

    await sup.finalize_spawned(aid, pid=4242)

    assert sup.stop_queue() == [4242]
    assert api.publisher.calls == []


@pytest.mark.parametrize("pid", [1, 0, -1])
async def test_a_spawn_report_must_name_a_single_process(api, monkeypatch, pid) -> None:
    """A report the service does not take (here one for an unknown agent) has
    its PID queued for the host loop to stop, so a PID that is not one
    process's (os.kill treats 0 and -1 as groups of processes) is refused."""
    sup = _cluster_supervisor(api, monkeypatch)

    resp = await api.client.post("/internal/spawned", json={"agent_id": str(uuid.uuid4()), "pid": pid})

    assert resp.status_code == 422
    assert sup.stop_queue() == []


async def test_a_launch_whose_publish_fails_is_stopped_and_marked_failed(api, monkeypatch) -> None:
    """A publish that fails with anything but a directory outage (here an invalid
    card) after the process was recorded: the process is stopped and the row
    set 'failed', so nothing keeps running unpublished."""
    sup = _cluster_supervisor(api, monkeypatch)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v1")

    def invalid_card(card, advertise_url=None):
        raise ValueError("card invalid")

    api.publisher.publish = invalid_card
    with pytest.raises(ValueError):
        await sup.finalize_spawned(aid, pid=111)

    row = await db.get_agent(aid)
    assert row["status"] == "failed" and row["ads_cid"] is None
    assert sup.stop_queue() == [111] and not sup._backend.has_process(aid)
    assert await sup.spawn_queue() == []


async def test_a_launch_whose_report_is_publishing_is_not_launched_again(api, monkeypatch) -> None:
    """While the report of a launch is still publishing, the row is still
    'image_ready', but the spawn queue leaves it out: a second process would
    compete with the first for the agent's port, and either could win it. A
    report that still comes for the row once it is live is stopped and
    publishes nothing; a reject then stops the first process, and the row can
    be queued again after a new approval."""
    sup = _cluster_supervisor(api, monkeypatch)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v1")
    started, release = _blocking_publish(api.publisher)
    first = asyncio.create_task(
        api.client.post("/internal/spawned", json={"agent_id": aid, "pid": 111})
    )
    await _until(started)
    assert (await db.get_agent(aid))["status"] == "image_ready"
    assert await sup.spawn_queue() == []
    second = asyncio.create_task(
        api.client.post("/internal/spawned", json={"agent_id": aid, "pid": 222})
    )
    await asyncio.sleep(0.05)
    release.set()
    assert (await first).status_code == 200 and (await second).status_code == 200

    assert sup.stop_queue() == [222]
    row = await db.get_agent(aid)
    assert row["status"] == "live" and row["ads_cid"] == "cid-1"
    resp = await api.client.post(f"/agents/{aid}/reject", json={"reason": "revoked"})
    assert resp.status_code == 200, resp.text
    assert sup.stop_queue() == [111]
    assert [c[:2] for c in api.publisher.calls] == [("publish", "cid-1"), ("unpublish", "cid-1")]
    await db.update_agent(aid, approved=True, status="image_ready", image_ref="sha256:v2")
    assert [e["agent_id"] for e in await sup.spawn_queue()] == [aid]


async def test_delete_during_a_health_restart_waits_and_removes_its_card(api, monkeypatch) -> None:
    """A health-loop restart attempt holds the agent lock: a delete waits for it,
    then removes the card it published; no card is left behind."""
    monkeypatch.setattr(sup_mod, "_BACKOFF_BASE_SECONDS", 0.0)
    aid = await _live(api)
    api.backend.alive.clear()  # the agent crashed
    started, release = _blocking_publish(api.publisher)
    await api.sup._check_all_live_agents()
    await _until(started)  # the restart is publishing its new card

    delete = asyncio.create_task(api.client.delete(f"/agents/{aid}"))
    await asyncio.sleep(0.1)
    assert not delete.done()
    release.set()
    assert (await delete).status_code == 204

    published = {c[1] for c in api.publisher.calls if c[0] == "publish"}
    unpublished = {c[1] for c in api.publisher.calls if c[0] == "unpublish"}
    assert published == unpublished == {"cid-1", "cid-2"}
    assert await db.get_agent(aid) is None
    assert api.backend.alive == set()


async def test_health_restart_queued_during_a_reject_leaves_the_agent_rejected(
    api, monkeypatch
) -> None:
    """The health loop can see the row still 'live' while a reject is stopping the
    process. The restart it starts then finds the agent rejected and does nothing:
    the row stays in the pre-approval state, not 'failed' (which the startup purge
    deletes)."""
    aid = await _live(api)
    monkeypatch.setattr(sup_mod, "_BACKOFF_BASE_SECONDS", 0.01)
    in_stop = asyncio.Event()
    stop = api.backend.stop

    async def slow_stop(agent_id):
        await stop(agent_id)  # the process is gone...
        in_stop.set()
        await asyncio.sleep(0.05)  # ...while the stop waits out its grace period

    api.backend.stop = slow_stop
    reject = asyncio.create_task(
        api.client.post(f"/agents/{aid}/reject", json={"reason": "revoked"})
    )
    await asyncio.wait_for(in_stop.wait(), 5)
    await api.sup._check_all_live_agents()
    restart = api.sup._restart_tasks[aid]
    assert (await reject).status_code == 200
    await restart

    row = await db.get_agent(aid)
    assert row["status"] == "registered" and row["approved"] is False
    assert api.backend.spawned == [aid]
    await init_fake_db()  # the startup purge
    assert await db.get_agent(aid) is not None


async def test_health_restart_after_an_api_restart_leaves_the_new_process(api, monkeypatch) -> None:
    aid = await _live(api)
    # Long enough for the API restart below to finish while the health restart
    # is still in its backoff sleep.
    monkeypatch.setattr(sup_mod, "_BACKOFF_BASE_SECONDS", 0.5)
    api.backend.alive.clear()
    await api.sup._check_all_live_agents()
    restart = api.sup._restart_tasks[aid]
    api.sup._restart_tasks.clear()  # as if created after the API restart's stop
    assert (await api.client.post(f"/agents/{aid}/restart")).status_code == 200
    assert not restart.done()

    await restart

    assert api.backend.spawned == [aid, aid] and api.backend.alive == {aid}
    assert (await db.get_agent(aid))["status"] == "live"


async def test_health_restart_of_a_live_row_without_approval_returns_it_to_approval(
    api, monkeypatch
) -> None:
    """A revoke whose backend stop raised leaves the row 'live' without approval.
    The health loop's restart finishes the revoke instead of starting a new
    restart on every poll."""
    aid = await _live(api)
    cid = (await db.get_agent(aid))["ads_cid"]
    await db.update_agent(aid, approved=False)
    api.backend.alive.clear()
    monkeypatch.setattr(sup_mod, "_BACKOFF_BASE_SECONDS", 0.0)

    await api.sup.restart_with_backoff(aid)

    row = await db.get_agent(aid)
    assert row["status"] == "registered" and row["approved"] is False
    assert row["ads_cid"] is None and ("unpublish", cid) in api.publisher.calls
    assert api.backend.spawned == [aid]


async def test_agent_locks_are_dropped_once_unused(api) -> None:
    aid = await _live(api)
    assert (await api.client.delete(f"/agents/{uuid.uuid4()}")).status_code == 404
    assert (await api.client.delete(f"/agents/{aid}")).status_code == 204
    gc.collect()
    assert dict(sup_mod._agent_locks) == {}


# ── Approval on a build backend always goes through the build ────────────────────


async def test_second_approval_while_building_is_refused(api, monkeypatch) -> None:
    sup = _cluster_supervisor(api, monkeypatch)
    aid = await _uploaded(api)
    revision = (await api.client.get(f"/agents/{aid}/handler")).json()["revision"]

    first, second = await asyncio.gather(
        api.client.post(f"/agents/{aid}/approve", json={"revision": revision}),
        api.client.post(f"/agents/{aid}/approve", json={"revision": revision}),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    refused = first if first.status_code == 409 else second
    assert "in progress (building)" in refused.json()["detail"]
    assert "If the build failed, restart the agent" in refused.json()["detail"]
    assert sup._commit_to_github.await_count == 1
    row = await db.get_agent(aid)
    assert row["status"] == "building" and row["ads_cid"] is None
    await db.update_agent(aid, status="image_ready")
    resp = await api.client.post(f"/agents/{aid}/approve")
    assert resp.status_code == 409 and "in progress (image_ready)" in resp.json()["detail"]
    assert api.publisher.calls == [] and sup._backend._procs == {}


@pytest.mark.parametrize("status", ["live", "failed", "stopped"])
async def test_reapproval_on_a_build_backend_starts_nothing_and_restart_rebuilds(
    api, monkeypatch, status
) -> None:
    """There is no direct spawn of uploaded code on the cluster or Cloud Run: an
    approval of an agent not waiting for its first build publishes nothing and
    starts nothing (on the cluster that would be a card with no process, on Cloud
    Run whatever ':latest' holds). A restart sends it through the build."""
    sup = _cluster_supervisor(api, monkeypatch)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status=status)

    resp = await api.client.post(f"/agents/{aid}/approve")

    assert resp.status_code == 409 and "restart it" in resp.json()["detail"]
    assert (await db.get_agent(aid))["status"] == status
    assert api.publisher.calls == []
    assert (await api.client.post(f"/agents/{aid}/restart")).status_code == 200
    assert (await db.get_agent(aid))["status"] == "building"
    assert sup._commit_to_github.await_count == 1


@pytest.mark.parametrize(
    "status, cid", [("stopped", "cid-1"), ("live", "cid-1"), ("image_ready", None)]
)
async def test_approval_of_an_unapproved_row_in_another_status_goes_through_the_build(
    api, monkeypatch, status, cid
) -> None:
    """A row without approval that is not 'registered' (a delete the directory
    did not confirm, a revoke whose stop raised, an earlier version's upload) is
    approved in one call: what runs is stopped, its card removed, and the
    approval starts the build."""
    sup = _cluster_supervisor(api, monkeypatch)
    aid = await _uploaded(api)
    await db.update_agent(aid, status=status, image_ref="sha256:v1", ads_cid=cid)

    resp = await api.client.post(f"/agents/{aid}/approve")

    assert resp.status_code == 200, resp.text
    row = await db.get_agent(aid)
    assert row["status"] == "building" and row["approved"] is True
    assert row["image_ref"] is None and row["ads_cid"] is None
    assert sup._commit_to_github.await_count == 1
    assert api.publisher.calls == ([("unpublish", cid)] if cid else [])


async def test_cluster_approval_without_build_credentials_starts_nothing(api, monkeypatch) -> None:
    sup = _cluster_supervisor(api, monkeypatch)
    monkeypatch.delenv("GITHUB_TOKEN")
    aid = await _uploaded(api)

    resp = await api.client.post(f"/agents/{aid}/approve")

    assert resp.status_code == 503 and "GITHUB_TOKEN" in resp.json()["detail"]
    row = await db.get_agent(aid)
    assert row["status"] == "registered" and row["approved"] is False
    assert api.publisher.calls == [] and sup._commit_to_github.await_count == 0


async def test_each_approval_commits_a_changed_build_context(api, monkeypatch) -> None:
    """The build workflow builds the agent directories a push changed, so the
    same code approved again (after a reject) must still change the commit."""
    _cluster_env(monkeypatch)
    api.backend.owns_build = True
    commits: list[dict[str, str]] = []

    async def commit(repo, token, files, message, *, directory):
        commits.append(files)

    monkeypatch.setattr(sup_mod, "_github_commit_files", commit)
    aid = await _uploaded(api)
    revision = (await api.client.get(f"/agents/{aid}/handler")).json()["revision"]
    assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200
    assert (await api.client.post(f"/agents/{aid}/reject", json={"reason": "x"})).status_code == 200
    assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200

    first, second = commits
    marker = f"registered_agents/{_image_slug(CUSTOM_PAYLOAD['name'])}/.approval"
    assert first.keys() == second.keys()
    assert {path for path in first if first[path] != second[path]} == {marker}
    assert json.loads(first[marker])["revision"] == revision


class _GitHub:
    """The Git Data API calls a commit makes, on one branch held in memory. A
    new tree starts from ``base_tree``: entries the request does not name are
    kept, and an entry with ``sha: None`` is deleted."""

    def __init__(self, files: dict[str, str]) -> None:
        self.trees = {"t0": dict(files)}
        self.commits = {"c0": "t0"}
        self.head = "c0"

    @property
    def files(self) -> dict[str, str]:
        return self.trees[self.commits[self.head]]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/repos/example-org/agents", 1)[1]
        body = json.loads(request.content) if request.content else {}
        if path == "":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": self.head}})
        if path.startswith("/git/commits/"):
            return httpx.Response(200, json={"tree": {"sha": self.commits[path.rsplit("/", 1)[1]]}})
        if path.startswith("/git/trees/"):
            tree = self.trees[path.rsplit("/", 1)[1]]
            entries = [{"path": p, "mode": "100644", "type": "blob", "sha": "x"} for p in tree]
            return httpx.Response(200, json={"tree": entries, "truncated": False})
        if path == "/git/trees":
            tree = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                if entry.get("sha", "") is None:
                    tree.pop(entry["path"], None)
                else:
                    tree[entry["path"]] = entry["content"]
            sha = f"t{len(self.trees)}"
            self.trees[sha] = tree
            return httpx.Response(201, json={"sha": sha})
        if path == "/git/commits":
            sha = f"c{len(self.commits)}"
            self.commits[sha] = body["tree"]
            return httpx.Response(201, json={"sha": sha})
        if path == "/git/refs/heads/main":
            self.head = body["sha"]
            return httpx.Response(200, json={})
        return httpx.Response(404)


async def test_each_approval_commits_exactly_the_reviewed_tree(api, monkeypatch) -> None:
    """Files of an earlier approval that the new upload no longer has are removed
    from the agent's build directory, which the image build copies whole."""
    _cluster_env(monkeypatch)
    api.backend.owns_build = True
    base = f"registered_agents/{_image_slug(CUSTOM_PAYLOAD['name'])}"
    github = _GitHub({"registered_agents/other-agent/src/handler.py": "kept\n"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        sup_mod.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(github.handle), **kw),
    )
    aid = await _uploaded(api, _zip(V1_TREE))
    assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200
    assert f"{base}/src/mylib/__init__.py" in github.files

    resp = await api.client.post(f"/agents/{aid}/handler", files=_py(V2_HANDLER_SRC))
    assert resp.status_code == 200, resp.text
    assert (await api.client.post(f"/agents/{aid}/approve")).status_code == 200

    src = {p for p in github.files if p.startswith(f"{base}/src/")}
    assert src == {f"{base}/src/handler.py"}
    assert github.files[f"{base}/src/handler.py"] == V2_HANDLER_SRC
    assert f"{base}/Dockerfile" in github.files
    assert github.files["registered_agents/other-agent/src/handler.py"] == "kept\n"


# ── Startup card retry ────────────────────────────────────────────────────────────


async def test_startup_keeps_the_card_id_of_a_building_row_so_its_build_advances(
    api, monkeypatch
) -> None:
    """Clearing the CID would move updated_at past an image pushed before the
    restart, and the build poll would wait for a newer one forever."""
    _cluster_env(monkeypatch)
    aid = await _uploaded(api)
    await db.update_agent(aid, approved=True, status="building", ads_cid="cid-old")
    pushed_at = (await db.get_agent(aid))["updated_at"]  # built right after approval

    async def image_exists(image, since=None):
        return since is not None and since <= pushed_at

    async def digest(image, tag="latest"):
        return "sha256:v1"

    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", image_exists)
    monkeypatch.setattr(sup_mod, "_image_digest_on_dockerhub", digest)
    await asyncio.sleep(0.01)  # the service restarts after the push
    await api.sup.recover_live_agents()
    assert (await db.get_agent(aid))["ads_cid"] == "cid-old"

    await api.sup._check_building_agents()

    row = await db.get_agent(aid)
    assert row["status"] == "image_ready" and row["image_ref"] == "sha256:v1"


async def test_startup_skips_the_card_retry_when_the_directory_does_not_answer(api) -> None:
    """One quick probe instead of one dirctl timeout per recorded card."""
    first = await _uploaded(api)
    resp = await api.client.post("/agents", json={**CUSTOM_PAYLOAD, "name": "Second Agent"})
    second = resp.json()["id"]
    await db.update_agent(first, status="stopped", ads_cid="cid-a")
    await db.update_agent(second, status="failed", ads_cid="cid-b")
    api.publisher.up = False

    await api.sup.recover_live_agents()

    assert api.publisher.calls == [("healthcheck",)]
    assert (await db.get_agent(first))["ads_cid"] == "cid-a"
    assert (await db.get_agent(second))["ads_cid"] == "cid-b"
