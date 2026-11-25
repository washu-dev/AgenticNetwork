"""Custom-agent images built with Cloud Build, and uploads kept in Cloud Storage.

With AGENT_BUILDER=cloudbuild on Cloud Run, an approval uploads the build context
to the bucket and creates a Cloud Build build; the build poll (or a build notice
at POST /internal/build-events) asks Cloud Build for its state and deploys the
pushed digest. Uploads are saved to the bucket and restored on a new instance.
No GCP: the bucket is a dict and Cloud Build's REST API is faked at
gcp_build._request.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from registration_service import db, gcp_build, server
from registration_service import supervisor as sup_mod
from registration_service.backend import CloudRunBackend
from registration_service.fake_db import reset_fake_db
from registration_service.server import app
from registration_service.supervisor import Supervisor
from registration_service.tests.conftest import CUSTOM_PAYLOAD, VALID_HANDLER_SRC
from registration_service.tests.test_cloudrun_backend import _FakeCloudRunClient, _FakePublisher

REGISTRY = "us-central1-docker.pkg.dev/example-project/agenticnetwork"
IMAGE = f"{REGISTRY}/agenticnetwork-custom-test-custom-agent"


def _members(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


# ── Pure helpers ────────────────────────────────────────────────────────────────


def test_tar_round_trip(tmp_path):
    files = {"Dockerfile": b"FROM python:3.11\n", "src/pkg/mod.py": b"x = 1\n", "src/data.bin": b"\x00\xff"}
    data = gcp_build.tar_files(files)
    assert gcp_build.tar_files(files) == data  # same files, same bytes
    gcp_build.untar_to(data, tmp_path / "out")
    assert {p.relative_to(tmp_path / "out").as_posix(): p.read_bytes()
            for p in (tmp_path / "out").rglob("*") if p.is_file()} == files


def test_untar_refuses_paths_outside_the_destination(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape.py")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="outside"):
        gcp_build.untar_to(buf.getvalue(), tmp_path / "out")
    assert not (tmp_path / "escape.py").exists()


def test_build_request_tags_the_image_with_the_build_id(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "example-project")
    monkeypatch.setenv("CLOUD_BUILD_SERVICE_ACCOUNT", "agent-builder@example-project.iam.gserviceaccount.com")
    body = gcp_build.build_request(bucket="b", context_object="build-contexts/x/1.tar.gz", image=IMAGE, slug="x")
    assert body["source"] == {"storageSource": {"bucket": "b", "object": "build-contexts/x/1.tar.gz"}}
    assert body["images"] == [f"{IMAGE}:$BUILD_ID"]
    assert body["steps"][0]["args"] == ["build", "-t", f"{IMAGE}:$BUILD_ID", "."]
    assert body["serviceAccount"] == (
        "projects/example-project/serviceAccounts/agent-builder@example-project.iam.gserviceaccount.com"
    )
    # A user-specified build account needs an explicit logging option.
    assert body["options"]["logging"] == "CLOUD_LOGGING_ONLY"


# ── Through the API, with the real supervisor on the Cloud Run backend ──────────


class _FakeGCP:
    """The bucket (a dict) and Cloud Build's builds.create / builds.get."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.build_requests: list[dict] = []
        self.status = "QUEUED"

    async def put(self, bucket, name, data):
        self.objects[name] = data

    async def get(self, bucket, name):
        return self.objects.get(name)

    async def delete(self, bucket, name):
        self.objects.pop(name, None)

    async def request(self, method, url, **kwargs):
        req = httpx.Request(method, url)
        if method == "POST" and url.endswith("/projects/example-project/builds"):
            self.build_requests.append(kwargs["json"])
            return httpx.Response(200, json={"metadata": {"build": {"id": "build-123"}}}, request=req)
        if method == "GET" and url.endswith("/projects/example-project/builds/build-123"):
            build = {"id": "build-123", "status": self.status, "logUrl": "https://console/logs/build-123"}
            if self.status == "SUCCESS":
                build["results"] = {"images": [{"name": f"{IMAGE}:build-123", "digest": "sha256:abc"}]}
            return httpx.Response(200, json=build, request=req)
        return httpx.Response(404, request=req)


@pytest_asyncio.fixture
async def api(monkeypatch, tmp_path):
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path / "handlers"))
    monkeypatch.setenv("SPAWN_BACKEND", "cloudrun")
    monkeypatch.setenv("GCP_PROJECT", "example-project")
    monkeypatch.setenv("GCP_REGION", "us-central1")
    monkeypatch.setenv("AGENT_RUNTIME_SA", "agent-runtime@example-project.iam.gserviceaccount.com")
    monkeypatch.setenv("CHAT_APP_INVOKER_SA", "chat-app@example-project.iam.gserviceaccount.com")
    monkeypatch.setenv("AGENT_BUILDER", "cloudbuild")
    monkeypatch.setenv("AGENT_CODE_BUCKET", "example-agent-code")
    monkeypatch.setenv("IMAGE_REGISTRY", REGISTRY)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    gcp = _FakeGCP()
    monkeypatch.setattr(gcp_build, "gcs_put", gcp.put)
    monkeypatch.setattr(gcp_build, "gcs_get", gcp.get)
    monkeypatch.setattr(gcp_build, "gcs_delete", gcp.delete)
    monkeypatch.setattr(gcp_build, "_request", gcp.request)
    reset_fake_db()
    db._pool = None
    await db.init_db()
    cloud_run, publisher = _FakeCloudRunClient(), _FakePublisher()
    sup = Supervisor(backend=CloudRunBackend(client=cloud_run), publisher=publisher)
    monkeypatch.setattr(sup_mod, "_supervisor", sup)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            client=client, sup=sup, gcp=gcp, cloud_run=cloud_run, publisher=publisher,
            handlers=tmp_path / "handlers",
        )
    await db.close_db()
    reset_fake_db()
    db._pool = None


async def _uploaded(api) -> str:
    resp = await api.client.post("/agents", json=CUSTOM_PAYLOAD)
    assert resp.status_code == 201, resp.text
    aid = resp.json()["id"]
    resp = await api.client.post(
        f"/agents/{aid}/handler", files={"file": ("handler.py", VALID_HANDLER_SRC.encode(), "text/x-python")}
    )
    assert resp.status_code == 200, resp.text
    return aid


async def _approved(api) -> str:
    aid = await _uploaded(api)
    resp = await api.client.post(f"/agents/{aid}/approve")
    assert resp.status_code == 200, resp.text
    return aid


@pytest.mark.asyncio
async def test_upload_is_saved_and_restored_on_a_new_instance(api):
    aid = await _uploaded(api)
    saved = _members(api.gcp.objects[f"uploads/{aid}.tar.gz"])
    assert VALID_HANDLER_SRC.encode() in saved.values()

    shutil.rmtree(api.handlers / aid)  # a new Cloud Run instance: the local copy is gone
    resp = await api.client.get(f"/agents/{aid}/handler")
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == VALID_HANDLER_SRC


@pytest.mark.asyncio
async def test_approval_submits_the_build_context_to_cloud_build(api):
    aid = await _uploaded(api)
    shutil.rmtree(api.handlers / aid)  # approved on a later instance than the upload
    resp = await api.client.post(f"/agents/{aid}/approve")
    assert resp.status_code == 200, resp.text

    row = await db.get_agent(aid)
    assert row["status"] == "building"
    assert row["build_commit"] == "build-123"

    (request,) = api.gcp.build_requests
    assert request["images"] == [f"{IMAGE}:$BUILD_ID"]
    context_object = request["source"]["storageSource"]["object"]
    assert context_object.startswith("build-contexts/test-custom-agent/")
    context = _members(api.gcp.objects[context_object])
    assert "Dockerfile" in context
    assert VALID_HANDLER_SRC.encode() in [v for k, v in context.items() if k.startswith("src/")]
    assert "agent_skeleton/__init__.py" in context
    assert "test-custom-agent.card.json" in context


@pytest.mark.asyncio
async def test_build_poll_waits_then_deploys_the_built_digest(api):
    aid = await _approved(api)

    api.gcp.status = "WORKING"
    await api.sup._check_building_agents()
    assert (await db.get_agent(aid))["status"] == "building"
    assert api.cloud_run.created == []

    api.gcp.status = "SUCCESS"
    await api.sup._check_building_agents()
    row = await db.get_agent(aid)
    assert row["status"] == "live"
    assert row["image_ref"] == "sha256:abc"
    assert api.cloud_run.created[0]["image"] == f"{IMAGE}@sha256:abc"
    assert len(api.publisher.published) == 1


@pytest.mark.asyncio
async def test_a_failed_build_marks_the_agent_failed(api):
    aid = await _approved(api)
    api.gcp.status = "FAILURE"
    await api.sup._check_building_agents()
    assert (await db.get_agent(aid))["status"] == "failed"
    assert api.cloud_run.created == []


@pytest.mark.asyncio
async def test_a_build_notice_checks_builds_now(api):
    aid = await _approved(api)
    api.gcp.status = "SUCCESS"
    resp = await api.client.post("/internal/build-events", json={"message": {"data": "e30="}})
    assert resp.status_code == 204
    for task in list(server._build_checks):
        await task
    assert (await db.get_agent(aid))["status"] == "live"


@pytest.mark.asyncio
async def test_delete_removes_the_saved_upload(api):
    aid = await _uploaded(api)
    resp = await api.client.delete(f"/agents/{aid}")
    assert resp.status_code == 204, resp.text
    assert f"uploads/{aid}.tar.gz" not in api.gcp.objects


@pytest.mark.asyncio
async def test_cloud_build_without_a_bucket_refuses_the_approval(api, monkeypatch):
    aid = await _uploaded(api)
    monkeypatch.delenv("AGENT_CODE_BUCKET")
    resp = await api.client.post(f"/agents/{aid}/approve")
    assert resp.status_code == 503
    assert "AGENT_CODE_BUCKET" in resp.json()["detail"]
    assert api.gcp.build_requests == []
