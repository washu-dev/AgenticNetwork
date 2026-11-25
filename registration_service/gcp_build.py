"""Cloud Storage and Cloud Build for custom agents on Cloud Run.

Two jobs, both gated on ``AGENT_CODE_BUCKET``:

* **Keep uploaded code.** A Cloud Run instance's disk is gone when the instance
  stops, and the registration service scales to zero, so an upload kept only in
  ``HANDLER_FILES_DIR`` would be lost before an admin approves it. Each accepted
  upload is also saved as ``uploads/<agent id>.tar.gz`` and restored when a request
  needs it (`save_upload`, `restore_upload`).
* **Build images** (``AGENT_BUILDER=cloudbuild``). An approval uploads the agent's
  build context as ``build-contexts/<slug>/<timestamp>.tar.gz`` and creates a Cloud
  Build build from it that pushes ``<IMAGE_REGISTRY>/agenticnetwork-custom-<slug>:<build id>``
  (`submit_build`, `get_build`). This replaces the GitHub commit → GitHub Action →
  Docker Hub path on Cloud Run; the cluster keeps that path.

Both use the REST APIs with the runtime's Application Default Credentials
(``google-auth``, installed with ``google-cloud-run``), imported lazily so the rest
of the service and the test suite run without GCP. Tests replace `_request`.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from registration_service.archive import list_tree

logger = logging.getLogger(__name__)

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_STORAGE = "https://storage.googleapis.com"
_CLOUD_BUILD = "https://cloudbuild.googleapis.com/v1"

# Cloud Build statuses that end a build without an image.
FAILED_STATUSES = frozenset({"FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"})


def code_bucket() -> str | None:
    """The bucket that keeps uploaded code and build contexts, or None (local disk only)."""
    return os.getenv("AGENT_CODE_BUCKET") or None


def cloud_build_enabled() -> bool:
    """True when approvals on Cloud Run build images with Cloud Build."""
    return os.getenv("SPAWN_BACKEND") == "cloudrun" and os.getenv("AGENT_BUILDER") == "cloudbuild"


# ── Auth and HTTP ──────────────────────────────────────────────────────────────

_credentials: Any = None
_token_lock = asyncio.Lock()


async def _auth_header() -> dict[str, str]:
    """A bearer header from Application Default Credentials (refreshed when stale)."""
    global _credentials
    async with _token_lock:
        if _credentials is None:
            import google.auth  # lazy: only on GCP

            _credentials, _ = google.auth.default(scopes=[_SCOPE])
        if not _credentials.valid:
            from google.auth.transport.requests import Request

            await asyncio.to_thread(_credentials.refresh, Request())
        return {"Authorization": f"Bearer {_credentials.token}"}


async def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    headers = {**(await _auth_header()), **kwargs.pop("headers", {})}
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await client.request(method, url, headers=headers, **kwargs)


# ── Cloud Storage ──────────────────────────────────────────────────────────────

async def gcs_put(bucket: str, name: str, data: bytes) -> None:
    resp = await _request(
        "POST",
        f"{_STORAGE}/upload/storage/v1/b/{bucket}/o",
        params={"uploadType": "media", "name": name},
        content=data,
        headers={"Content-Type": "application/gzip"},
    )
    resp.raise_for_status()


async def gcs_get(bucket: str, name: str) -> bytes | None:
    """The object's bytes, or None if it does not exist."""
    resp = await _request(
        "GET", f"{_STORAGE}/storage/v1/b/{bucket}/o/{quote(name, safe='')}", params={"alt": "media"}
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


async def gcs_delete(bucket: str, name: str) -> None:
    """Delete the object's live version; a missing object counts as deleted."""
    resp = await _request("DELETE", f"{_STORAGE}/storage/v1/b/{bucket}/o/{quote(name, safe='')}")
    if resp.status_code != 404:
        resp.raise_for_status()


# ── Tarballs ───────────────────────────────────────────────────────────────────

def tar_files(files: dict[str, bytes]) -> bytes:
    """A gzipped tarball of ``files`` (relative posix path → bytes), sorted and with
    fixed metadata so the same files give the same bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in sorted(files):
            data = files[rel]
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def tar_tree(root: Path) -> bytes:
    """A gzipped tarball of the stored project tree (the files `list_tree` returns)."""
    return tar_files({rel: (root / rel).read_bytes() for rel in list_tree(root)})


def untar_to(data: bytes, dest: Path) -> None:
    """Unpack a tarball made by `tar_files` into ``dest``: regular files only, and
    none may land outside ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            target = (root / member.name).resolve()
            if root not in target.parents:
                raise ValueError(f"tar member outside the destination: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            target.write_bytes(src.read() if src else b"")


# ── Uploaded code ──────────────────────────────────────────────────────────────

def _upload_object(agent_id: str) -> str:
    return f"uploads/{agent_id}.tar.gz"


async def save_upload(agent_id: str, tree: Path) -> None:
    """Save an accepted upload's tree to the bucket (no-op without one)."""
    bucket = code_bucket()
    if bucket:
        await gcs_put(bucket, _upload_object(agent_id), tar_tree(tree))


async def restore_upload(agent_id: str, handler_dir: Path) -> bool:
    """Put the saved upload back in ``handler_dir`` when the local copy is gone (a new
    Cloud Run instance). True if the directory exists afterwards."""
    if handler_dir.exists():
        return True
    bucket = code_bucket()
    if not bucket:
        return False
    data = await gcs_get(bucket, _upload_object(agent_id))
    if data is None:
        return False
    staging = handler_dir.with_name(f".restore-{handler_dir.name}-{time.monotonic_ns()}")
    untar_to(data, staging)
    try:
        staging.rename(handler_dir)
    except OSError:
        # Another request restored it first.
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
    logger.info("upload_restored agent_id=%s", agent_id)
    return handler_dir.exists()


async def delete_upload(agent_id: str) -> None:
    """Best-effort removal of the saved upload (bucket versioning may keep a copy)."""
    bucket = code_bucket()
    if not bucket:
        return
    try:
        await gcs_delete(bucket, _upload_object(agent_id))
    except Exception as exc:
        logger.warning("upload_delete_failed agent_id=%s error=%s", agent_id, exc)


# ── Cloud Build ────────────────────────────────────────────────────────────────

def _project() -> str:
    project = os.getenv("GCP_PROJECT")
    if not project:
        raise RuntimeError("GCP_PROJECT is required for Cloud Build")
    return project


def build_request(*, bucket: str, context_object: str, image: str, slug: str) -> dict[str, Any]:
    """The Cloud Build request: one docker build of the uploaded context, pushed as
    ``<image>:$BUILD_ID`` (the build's own id, so the tag names this build)."""
    tagged = f"{image}:$BUILD_ID"
    body: dict[str, Any] = {
        "source": {"storageSource": {"bucket": bucket, "object": context_object}},
        "steps": [{"name": "gcr.io/cloud-builders/docker", "args": ["build", "-t", tagged, "."]}],
        "images": [tagged],
        "timeout": f"{int(os.getenv('CLOUD_BUILD_TIMEOUT_SECONDS', '1800'))}s",
        "tags": ["custom-agent", slug[:128]],
        # Required when the build runs as a user-specified service account.
        "options": {"logging": "CLOUD_LOGGING_ONLY"},
    }
    service_account = os.getenv("CLOUD_BUILD_SERVICE_ACCOUNT")
    if service_account:
        body["serviceAccount"] = f"projects/{_project()}/serviceAccounts/{service_account}"
    return body


async def submit_build(*, slug: str, image: str, files: dict[str, bytes]) -> str:
    """Upload the build context and start its build; return the build's id."""
    bucket = code_bucket()
    if not bucket:
        raise RuntimeError("AGENT_CODE_BUCKET is required for Cloud Build")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    context_object = f"build-contexts/{slug}/{stamp}.tar.gz"
    await gcs_put(bucket, context_object, tar_files(files))
    # Global endpoint: a regional create was refused in di2-agenticweb (2026-11-16).
    resp = await _request(
        "POST",
        f"{_CLOUD_BUILD}/projects/{_project()}/builds",
        json=build_request(bucket=bucket, context_object=context_object, image=image, slug=slug),
    )
    resp.raise_for_status()
    build_id = resp.json()["metadata"]["build"]["id"]
    logger.info("cloud_build_submitted slug=%s build_id=%s context=gs://%s/%s", slug, build_id, bucket, context_object)
    return build_id


async def get_build(build_id: str) -> dict[str, Any]:
    """The build's status, the pushed image's digest (when it succeeded) and its log URL."""
    resp = await _request("GET", f"{_CLOUD_BUILD}/projects/{_project()}/builds/{build_id}")
    resp.raise_for_status()
    build = resp.json()
    images = (build.get("results") or {}).get("images") or []
    return {
        "status": build.get("status"),
        "digest": images[0].get("digest") if images else None,
        "log_url": build.get("logUrl"),
    }
