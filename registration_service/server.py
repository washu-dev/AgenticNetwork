"""FastAPI server — REST API for the registration service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from agentnet.directory.publisher import DirectoryPublisher
from registration_service import db
from registration_service.archive import (
    ArchiveError,
    list_tree,
    tree_revision,
    unpack_archive,
    zip_tree,
)
from registration_service.backend import _cloudrun_service_id, _handler_files_dir, _image_slug
from registration_service.db import get_pool
from registration_service.lifespan import lifespan
from registration_service.models import (
    AgentResponse,
    ApproveRequest,
    CustomConfig,
    OASFSkill,
    RegisterRequest,
    RejectRequest,
    SpawnedReport,
    carries_upload,
    upload_spec,
)
from registration_service.credential_catalog import CATALOG as CREDENTIAL_CATALOG
from registration_service.flags import fake_db_enabled, fake_supervisor_enabled
from registration_service.oasf_skills import OASF_SKILLS, has_routable_skill
from registration_service.port_pool import assign_port
from registration_service.supervisor import (
    LaunchRefused,
    _agent_lock,
    _deserialize_config,
    _uploads_code,
    get_supervisor,
)
from registration_service.validator import validate_handler, validate_tool_module


# `_uploads_code(agent)` (from supervisor, shared with cluster startup recovery) is
# the gate for the whole register-unapproved → upload → approve lifecycle and the
# handler upload/review/download/approve/reject endpoints below.


def _handler_dir(agent_id: Any) -> Path:
    return _handler_files_dir() / str(agent_id)


# The per-agent lock (supervisor._agent_lock) is shared with the supervisor's
# launch paths. The endpoints take it with the canonical id (_canonical_id), so
# every spelling of the UUID shares one lock, one handler directory and one
# backend process key.


def _replace_dir(new: Path, target: Path) -> None:
    """Put ``new`` in place of ``target`` by renames (same filesystem), so the
    stored tree is never a mix of the old upload and the new one."""
    old = target.with_name(f".replaced-{target.name}-{uuid.uuid4().hex}")
    if target.exists():
        target.rename(old)
    try:
        new.rename(target)
    except OSError:
        if old.exists():
            old.rename(target)
        raise
    shutil.rmtree(old, ignore_errors=True)


async def _revoke_approval(agent_id: str) -> None:
    """Return an uploaded-code agent to the pre-approval state. The caller holds
    the agent's lock (_agent_lock).

    Approval is revoked first, so no spawn can start meanwhile. Unless the agent
    is 'registered' or 'stopped' with no image and no card, it is stopped and
    its card removed: a launch that failed part way (a Cloud Run deploy whose
    service was created) can leave it running, and so can a stop that failed at
    startup (_reset_revoked_rows), which this then retries. A 'building' or
    'image_ready' one leaves the build poll and the spawn queue with its
    status. image_ref is cleared, so the next approval goes through the normal
    build path.
    """
    uid = _parse_uuid(agent_id)
    agent = await db.get_agent(uid)
    if agent is None:
        raise HTTPException(status_code=404, detail="Uploaded-code agent not found")
    await db.update_agent(uid, approved=False)
    if agent["status"] not in ("registered", "stopped") or agent.get("image_ref") or agent.get("ads_cid"):
        await get_supervisor().stop_and_unpublish(agent_id)
    await db.update_agent(uid, status="registered", image_ref=None)


# ZIP local-file-header magic; used as a fallback when the filename lacks a suffix.
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _is_zip_upload(filename: str | None, raw: bytes) -> bool:
    """Treat the upload as a .zip if its name says so, or its bytes start with PK magic."""
    if filename and filename.lower().endswith(".zip"):
        return True
    if filename and filename.lower().endswith(".py"):
        return False
    return raw[:4] in _ZIP_MAGIC


_SHIB_HEADER_PRIORITY = ("mail", "eppn", "remote-user")


def _admin_emails() -> set[str]:
    raw = os.getenv("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _require_admin(request: Request) -> None:
    """Fail-closed admin check on the forwarded SSO identity (P4f).

    The chat app passes the approver's requests through, forwarding the Apache-injected
    Shibboleth headers. Admin = that identity ∈ ADMIN_EMAILS; an empty allowlist means
    no admins. Dev/test escape hatch: FAKE_DB=1 leaves it open (no Shibboleth locally).
    """
    if fake_db_enabled():
        return
    email = next(
        (request.headers.get(h, "").strip().lower() for h in _SHIB_HEADER_PRIORITY
         if request.headers.get(h, "").strip()),
        None,
    )
    if email is None or email not in _admin_emails():
        raise HTTPException(status_code=403, detail="Admin access required")

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="AgenticNetwork Registration Service",
    description="Register LLM agents and API proxies into the AgenticNetwork.",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    """Liveness only: the process answers HTTP. See /ready for dependencies."""
    return {"status": "ok"}


# /ready probe timeouts (the chat app's values). dirctl is a subprocess, so the
# directory check gets longer.
_READY_DB_TIMEOUT_SECONDS = 2.0
_READY_DIRECTORY_TIMEOUT_SECONDS = 5.0


def _check_error(exc: BaseException, timeout: float) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return f"timed out after {timeout:g}s"
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:500]


async def _check_db() -> dict[str, Any]:
    """The store answers a trivial query (postgres, or the in-memory fake)."""
    backend = "fake" if fake_db_enabled() else "postgres"

    async def probe() -> None:
        async with (await get_pool()).acquire() as conn:
            await conn.fetchval("SELECT 1")

    try:
        await asyncio.wait_for(probe(), timeout=_READY_DB_TIMEOUT_SECONDS)
    except Exception as exc:
        return {"status": "error", "backend": backend,
                "error": _check_error(exc, _READY_DB_TIMEOUT_SECONDS)}
    return {"status": "ok", "backend": backend}


async def _check_directory() -> dict[str, Any]:
    """The directory answers one cheap dirctl read, when agents get published."""
    if fake_supervisor_enabled():
        return {"status": "not_configured", "reason": "FAKE_SUPERVISOR: agents are not published"}
    publisher = DirectoryPublisher()
    if not publisher.server_addr:
        # Agents then go live unpublished (ads_cid null); that is a configuration
        # choice, not an outage, so it doesn't fail readiness.
        return {"status": "not_configured"}
    timeout = _READY_DIRECTORY_TIMEOUT_SECONDS
    try:
        # healthcheck's own subprocess timeout kills a hung dirctl.
        await asyncio.to_thread(publisher.healthcheck, timeout=timeout)
    except Exception as exc:
        return {"status": "error", "server_addr": publisher.server_addr,
                "error": _check_error(exc, timeout)}
    return {"status": "ok", "server_addr": publisher.server_addr}


@app.get("/ready", tags=["ops"])
async def ready() -> JSONResponse:
    """Readiness, in the chat app's shape: 200 when no check is in error, else 503.

    ``{"ready": bool, "checks": {"db": {...}, "directory": {...}}}``; each check has
    ``status`` ("ok", "not_configured" or "error") and, on error, ``error``. A
    dependency that is not configured is reported but does not fail readiness.
    """
    db_check, directory_check = await asyncio.gather(_check_db(), _check_directory())
    checks = {"db": db_check, "directory": directory_check}
    is_ready = all(check["status"] != "error" for check in checks.values())
    return JSONResponse(
        {"ready": is_ready, "checks": checks}, status_code=200 if is_ready else 503
    )


# ── Web UI ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=FileResponse, include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


# ── OASF skill list ────────────────────────────────────────────────────────────

@app.get("/oasf-skills", tags=["reference"])
async def list_oasf_skills() -> list[dict]:
    """Return the full flat OASF skill taxonomy available for registration."""
    return OASF_SKILLS


@app.get("/credential-catalog", tags=["reference"])
async def list_credential_catalog() -> list[dict]:
    """Known credential types a custom agent may declare (P4a). The registration
    form populates its type dropdown from this; ``custom:<slug>`` is also allowed."""
    return [
        {"type": e.type, "label": e.label, "kind": e.kind, "docs_url": e.docs_url}
        for e in CREDENTIAL_CATALOG
    ]


# ── Agent CRUD ─────────────────────────────────────────────────────────────────

@app.post("/agents", response_model=AgentResponse, status_code=201, tags=["agents"])
async def register_agent(req: RegisterRequest) -> AgentResponse:
    """Register a new agent and start it.

    Custom agents are created in a not-yet-approved state and are NOT started
    here — the handler file must be uploaded (POST /agents/{id}/handler) and an
    admin must approve (POST /agents/{id}/approve) before they spawn.
    """
    # Enforce unique names: the name's slug drives the registered_agents/ dir, the
    # Docker image tag, and the ADS routing key — duplicate names collide on all
    # three (e.g. two "Echo Test" agents share echo-test). The Cloud Run service id
    # sanitizes the slug further and cuts it at 63 characters ('weather_bot' and
    # 'Weather Bot' both become agent-weather-bot), and every deploy, stop and
    # health check names the service by it. Reject either clash up front.
    new_slug = _image_slug(req.name)
    new_service = _cloudrun_service_id(req.name)
    async with (await get_pool()).acquire() as conn:
        existing = await conn.fetch("SELECT * FROM agents ORDER BY created_at DESC")
    if any(
        _image_slug(r["name"]) == new_slug or _cloudrun_service_id(r["name"]) == new_service
        for r in existing
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"An agent with a conflicting name already exists (slug '{new_slug}', "
                f"Cloud Run service '{new_service}'): {req.name!r}"
            ),
        )

    # At least one skill must carry an id so the card gets a routable
    # 'oasf:<name>:<id>' tag (_build_agent_card) — otherwise it publishes to ADS
    # stored-but-not-routed (undiscoverable). Reject before spawning anything.
    if not has_routable_skill([s.model_dump() for s in req.oasf_skills]):
        raise HTTPException(
            status_code=400,
            detail="At least one oasf_skill must have an id (from GET /oasf-skills) "
            "so the agent is discoverable in ADS.",
        )

    if carries_upload(req.typed_config()):
        # Uploaded-code agents are created NOT-approved and are
        # NOT started here: the code must be uploaded (POST /agents/{id}/handler)
        # and an admin must approve (POST /agents/{id}/approve) before they spawn.
        agent = await db.create_agent(
            name=req.name,
            description=req.description,
            handler_type=req.canonical_handler_type,
            oasf_skills=[s.model_dump() for s in req.oasf_skills],
            config=req.typed_config().model_dump(),
            approved=False,
            # Deploy sizing for cloud backends (CloudRunBackend). Only uploaded-code
            # agents reach a cloud deploy path, so resources is persisted here; the
            # subprocess-only handler types below ignore it.
            resources=req.resources.model_dump() if req.resources else None,
        )
        return _row_to_response(agent)

    pool = await get_pool()
    agent_id = str(uuid.uuid4())
    # Held from the insert to the response: a delete or restart that names the
    # new agent waits for its first launch, then stops it and removes its card.
    async with _agent_lock(agent_id):
        async with pool.acquire() as conn:
            async with conn.transaction():
                typed_config = req.typed_config()
                oasf_skills_data = [s.model_dump() for s in req.oasf_skills]

                # Insert agent first (port nullable) so the FK in port_pool is satisfied.
                await conn.execute(
                    """
                    INSERT INTO agents
                        (id, name, description, handler_type, oasf_skills, config, status)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, 'registered')
                    """,
                    uuid.UUID(agent_id),
                    req.name,
                    req.description,
                    req.canonical_handler_type,
                    json.dumps(oasf_skills_data),
                    json.dumps(typed_config.model_dump()),
                )

                try:
                    port = await assign_port(conn, agent_id)
                except RuntimeError as exc:
                    raise HTTPException(status_code=503, detail=str(exc))

                await conn.execute(
                    "UPDATE agents SET port = $1 WHERE id = $2",
                    port,
                    uuid.UUID(agent_id),
                )

        try:
            await get_supervisor().spawn_and_publish(agent_id)
        except Exception as exc:
            logger.error("register_agent_spawn_failed agent_id=%s error=%s", agent_id, exc)
            raise HTTPException(status_code=500, detail=f"Agent registered but failed to start: {exc}")

        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agents WHERE id = $1", uuid.UUID(agent_id))

    return _row_to_response(row)


@app.get("/agents", response_model=list[AgentResponse], tags=["agents"])
async def list_agents() -> list[AgentResponse]:
    """List all registered agents and their current status."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM agents ORDER BY created_at DESC")
    return [_row_to_response(r) for r in rows]


@app.get("/agents/{agent_id}", response_model=AgentResponse, tags=["agents"])
async def get_agent(agent_id: str) -> AgentResponse:
    """Get a single agent by ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agents WHERE id = $1", _parse_uuid(agent_id)
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _row_to_response(row)


@app.delete("/agents/{agent_id}", status_code=204, tags=["agents"])
async def delete_agent(agent_id: str, request: Request, force: bool = False) -> Response:
    """Stop and unregister an agent.

    If the directory does not confirm removing the agent's card, the agent is
    stopped but its row (which holds the card's CID) is kept and the call fails
    with 503, so the delete can be retried once the directory is reachable.
    ``?force=true`` (admin-only) deletes the row and files even then; the CID of
    the card left in the directory is logged as a warning.
    """
    if force:
        _require_admin(request)
    agent_id = _canonical_id(agent_id)
    async with _agent_lock(agent_id):
        agent = await db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        removed = await get_supervisor().stop_and_unpublish(agent_id)
        if not removed and not force:
            raise HTTPException(
                status_code=503,
                detail="Directory unavailable: the agent is stopped but its card could "
                "not be removed. Try the delete again; an admin can force it "
                "(?force=true), which leaves the card in the directory.",
            )
        if not removed:
            logger.warning(
                "agent_force_deleted_card_left agent_id=%s cid=%s", agent_id, agent["ads_cid"]
            )
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agents WHERE id = $1", _parse_uuid(agent_id))

        # Clean up any uploaded handler files / per-agent venv.
        handler_dir = _handler_dir(agent_id)
        if handler_dir.exists():
            shutil.rmtree(handler_dir, ignore_errors=True)

    # Remove the committed build context from the agent-directory repo (uploaded-code
    # agents on the cluster build path only). Best-effort — never blocks the delete.
    if _uploads_code(agent):
        await get_supervisor().delete_github_context(agent["name"])
    return Response(status_code=204)


@app.post("/agents/{agent_id}/restart", response_model=AgentResponse, tags=["agents"])
async def restart_agent(agent_id: str) -> AgentResponse:
    """Manually restart an agent.

    Holds the agent's lock from the stop to the end of the spawn, so an upload
    cannot replace the files the new process is started from.
    """
    agent_id = _canonical_id(agent_id)
    pool = await get_pool()
    async with _agent_lock(agent_id):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agents WHERE id = $1", _parse_uuid(agent_id)
            )
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        await get_supervisor().stop_and_unpublish(agent_id)

        # Re-assign a port and set status back to registered before re-spawning.
        async with pool.acquire() as conn:
            async with conn.transaction():
                try:
                    port = await assign_port(conn, agent_id)
                except RuntimeError as exc:
                    raise HTTPException(status_code=503, detail=str(exc))
                await conn.execute(
                    "UPDATE agents SET port = $1, status = 'registered', updated_at = now() WHERE id = $2",
                    port,
                    _parse_uuid(agent_id),
                )

        try:
            await get_supervisor().spawn_and_publish(agent_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Restart failed: {exc}")

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agents WHERE id = $1", _parse_uuid(agent_id))
    return _row_to_response(row)


# ── Custom handler: upload / approve / reject ───────────────────────────────────

@app.post("/agents/{agent_id}/handler", status_code=200, tags=["custom"])
async def upload_handler(agent_id: str, file: UploadFile = File(...)) -> dict:
    """Upload (or re-upload) a custom agent's code — a single ``.py`` or a ``.zip``.

    - ``.py``  → stored as ``{handler_dir}/{entry_file}`` (single-file, as before).
    - ``.zip`` → validated + unpacked into ``{handler_dir}/`` preserving structure;
      the archive root is the project root, and ``entry_file`` must sit at it.
      Members under ``.git/``, ``.venv/`` or ``__pycache__/`` are not extracted
      (a warning says how many were skipped).

    The upload is unpacked and validated in a staging directory: a rejected one
    (422) leaves the stored files and the agent row as they were. The entry file is
    AST-validated (structural errors block with 422; advisory warnings are returned
    and shown to the approver). An accepted upload replaces the stored tree by
    rename and returns the agent to the pre-approval state (``_revoke_approval``):
    a running agent is stopped and unpublished until an admin approves the new code.
    """
    agent_id = _canonical_id(agent_id)
    agent = await db.get_agent(agent_id)
    if not agent or not _uploads_code(agent):
        raise HTTPException(status_code=404, detail="Uploaded-code agent not found")

    config = _deserialize_config(agent)
    entry_file = upload_spec(config).entry_file   # non-None: _uploads_code() passed

    raw = await file.read()
    is_zip = _is_zip_upload(file.filename, raw)
    handler_dir = _handler_dir(agent_id)
    # Next to handler_dir (same filesystem, so the swap below is a rename); the dot
    # prefix keeps it apart from the per-agent directories.
    staging = handler_dir.with_name(f".upload-{handler_dir.name}-{uuid.uuid4().hex}")
    staging.mkdir(parents=True)
    skipped: list[str] = []
    try:
        if is_zip:
            try:
                _, skipped = unpack_archive(raw, staging, entry_file)
            except ArchiveError as exc:
                raise HTTPException(status_code=422, detail={"errors": [str(exc)]})
            source = (staging / entry_file).read_text(encoding="utf-8", errors="replace")
        else:
            try:
                source = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(status_code=422, detail={"errors": ["File is not valid UTF-8 text"]})
            (staging / entry_file).write_text(source)

        # Validate the entry file only (structural blocks; the blocklist is advisory,
        # docs/DECISIONS.md D2).
        # An uploaded TOOL module checks the tool contract (TOOL_SCHEMAS/TOOL_REGISTRY or
        # build_tools); a custom handler checks the AgentHandler subclass. Which one is
        # decided by the config, not the handler_type name.
        if isinstance(config, CustomConfig):
            result = validate_handler(source, config.class_name)
        else:
            result = validate_tool_module(source)
        if not result.ok:
            raise HTTPException(status_code=422, detail={"errors": result.errors})

        async with _agent_lock(agent_id):
            await _revoke_approval(agent_id)
            _replace_dir(staging, handler_dir)
            await db.update_agent(
                _parse_uuid(agent_id),
                handler_file_path=str(handler_dir / entry_file),
                is_archive=is_zip,
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)  # gone already once swapped in
    warnings = list(result.warnings)
    if skipped:
        warnings.append(
            f"Skipped {len(skipped)} file(s) under .git/, .venv/ or __pycache__/: "
            "uploads never include these directories"
        )
    return {"status": "uploaded", "errors": [], "warnings": warnings}


@app.get("/agents/{agent_id}/handler", tags=["custom"])
async def get_handler_source(agent_id: str, request: Request) -> dict:
    """Admin-only: what the approver reviews before approving (the trust boundary).

    - single-file agent → the entry-file ``source`` (as before).
    - multi-file agent  → a ``manifest`` of the stored tree + a ``download_url``
      pointing at the zip (GET .../handler/download), so the whole project can be
      inspected. In both cases advisory ``warnings`` from the entry file are included.

    ``revision`` identifies this exact upload (``archive.tree_revision``); an
    approval that sends it back is refused if the code changed since. The read
    holds the agent lock, so no upload replaces the tree while it is read and
    the source, manifest and revision describe the same upload.
    """
    _require_admin(request)
    agent_id = _canonical_id(agent_id)
    async with _agent_lock(agent_id):
        agent = await db.get_agent(agent_id)
        if not agent or not _uploads_code(agent):
            raise HTTPException(status_code=404, detail="Uploaded-code agent not found")
        path = agent.get("handler_file_path")
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="No handler file uploaded")

        handler_dir = _handler_dir(agent_id)
        revision = tree_revision(handler_dir)
        entry_source = Path(path).read_text(encoding="utf-8", errors="replace")
        manifest = list_tree(handler_dir) if agent.get("is_archive") else None
    config = _deserialize_config(agent)
    spec = upload_spec(config)                    # non-None: _uploads_code() passed
    if isinstance(config, CustomConfig):
        warnings = validate_handler(entry_source, config.class_name).warnings
        credentials = [c.model_dump() for c in config.credentials]   # user creds it asks for (P4a)
    else:
        warnings = validate_tool_module(entry_source).warnings
        credentials: list = []   # uploaded tool modules declare no per-user creds (v1)

    if manifest is not None:
        return {
            "is_archive": True,
            "entry_file": Path(path).name,
            "manifest": manifest,
            "download_url": f"/agents/{agent_id}/handler/download",
            "revision": revision,
            "warnings": warnings,
            "system_packages": spec.system_packages,   # apt, flagged for review
            "credentials": credentials,
        }
    return {
        "is_archive": False,
        "source": entry_source,
        "handler_file_path": path,
        "revision": revision,
        "warnings": warnings,
        "system_packages": spec.system_packages,         # apt, flagged for review
        "credentials": credentials,
    }


@app.get("/agents/{agent_id}/handler/download", tags=["custom"])
async def download_handler_archive(agent_id: str, request: Request) -> Response:
    """Admin-only: download the stored project tree as a zip (multi-file inspection)."""
    _require_admin(request)
    agent_id = _canonical_id(agent_id)
    agent = await db.get_agent(agent_id)
    if not agent or not _uploads_code(agent):
        raise HTTPException(status_code=404, detail="Uploaded-code agent not found")
    handler_dir = _handler_dir(agent_id)
    if not agent.get("is_archive") or not handler_dir.exists():
        raise HTTPException(status_code=404, detail="No uploaded archive for this agent")
    data = zip_tree(handler_dir)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{agent_id}-handler.zip"'},
    )


@app.post("/agents/{agent_id}/approve", tags=["custom"])
async def approve_agent(
    agent_id: str, request: Request, body: ApproveRequest | None = None
) -> dict:
    """Admin-only: approve an uploaded-code agent and start it.

    The admin UI, and any caller that reviewed the code first, sends
    ``{"revision": ...}`` with the revision of the code it reviewed (from
    GET /agents/{id}/handler). If the stored code no longer has that revision
    (an upload landed after the review), nothing is approved and the call fails
    with 409. Without a revision, the code stored when the approval runs is
    approved. An agent already approved whose build or launch is in progress
    ('building', 'image_ready') is not approved again (409). An unapproved
    agent is first returned to the pre-approval state (``_revoke_approval``),
    which stops what may still run for it (left by a revoke, delete or startup
    stop that did not finish, or by an earlier version), so the approval starts
    from there. A launch the backend refuses (LaunchRefused: on a build
    backend, an agent not waiting for its first build, or missing build
    credentials) leaves the approval as it was and answers with the refusal's
    status (409 or 503).
    """
    _require_admin(request)
    agent_id = _canonical_id(agent_id)
    async with _agent_lock(agent_id):
        current = await db.get_agent(agent_id)
        if not current or not _uploads_code(current):
            raise HTTPException(status_code=404, detail="Uploaded-code agent not found")
        if not current.get("handler_file_path"):
            raise HTTPException(status_code=409, detail="No handler file uploaded yet")
        if current["approved"] and current["status"] in ("building", "image_ready"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Already approved; its build or launch is in progress ({current['status']}). "
                    "If the build failed, restart the agent to build it again, or upload "
                    "fixed code (which returns it to approval)."
                ),
            )
        reviewed = body.revision if body else None
        if reviewed is not None and tree_revision(_handler_dir(agent_id)) != reviewed:
            raise HTTPException(
                status_code=409,
                detail="The uploaded code changed since it was reviewed; review it again.",
            )
        if not current["approved"]:
            await _revoke_approval(agent_id)
        await db.update_agent(_parse_uuid(agent_id), approved=True)
        try:
            await get_supervisor().spawn_and_publish(agent_id)
        except LaunchRefused as exc:
            await db.update_agent(_parse_uuid(agent_id), approved=current["approved"])
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except Exception as exc:
            logger.error("approve_spawn_failed agent_id=%s error=%s", agent_id, exc)
            raise HTTPException(status_code=500, detail=f"Approved but failed to start: {exc}")
    return {"status": "ok"}


@app.post("/agents/{agent_id}/reject", tags=["custom"])
async def reject_agent(agent_id: str, body: RejectRequest, request: Request) -> dict:
    """Admin-only: reject a custom agent, or revoke an earlier approval.

    The agent returns to the pre-approval state (``_revoke_approval``): if it is
    running it is stopped and its card removed, and any queued build or spawn
    for it is dropped.
    """
    _require_admin(request)
    agent_id = _canonical_id(agent_id)
    agent = await db.get_agent(agent_id)
    if not agent or not _uploads_code(agent):
        raise HTTPException(status_code=404, detail="Uploaded-code agent not found")

    async with _agent_lock(agent_id):
        await _revoke_approval(agent_id)
    logger.info("agent_rejected agent_id=%s reason=%s", agent_id, body.reason)
    return {"status": "ok"}


# ── Internal endpoints (cluster host spawn loop, Note #8) ───────────────────────
# Localhost-only — consumed by deployment/spawn_agent_loop.py. No auth (the
# registration service binds only inside the allocation's node).

@app.get("/internal/spawn-queue", tags=["internal"])
async def internal_spawn_queue() -> list[dict]:
    return await get_supervisor().spawn_queue()


@app.post("/internal/spawned", tags=["internal"])
async def internal_spawned(body: SpawnedReport) -> dict:
    await get_supervisor().finalize_spawned(str(body.agent_id), body.pid)
    return {"status": "ok"}


@app.get("/internal/stop-queue", tags=["internal"])
async def internal_stop_queue() -> dict:
    return {"pids": get_supervisor().stop_queue()}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid UUID: {value!r}")


def _canonical_id(value: str) -> str:
    """The agent id in its one canonical spelling (lowercase, hyphenated), for
    the keys an id names: the agent lock, the handler directory, backend state."""
    return str(_parse_uuid(value))


_REDACTED = "[redacted]"


def _redact_url(url: Any) -> Any:
    """Hide credentials a URL may carry: userinfo and query-string values."""
    if not isinstance(url, str):
        return url
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{_REDACTED}@{netloc.rsplit('@', 1)[1]}"
    query = "&".join(f"{pair.split('=', 1)[0]}={_REDACTED}" for pair in parts.query.split("&") if pair)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _public_config(config: Any) -> Any:
    """The agent config as the API shows it: secret-bearing values replaced.

    Runtimes read the stored config from the database; API responses go to any
    signed-in user (the chat app passes ``/register/agents`` through), so they never
    carry a proxy's ``auth_header``, the values of an ``extra`` dict (top-level or
    ``tools.extra``; keys stay visible), or credentials embedded in a URL.
    """
    if not isinstance(config, dict):
        return config
    out = dict(config)
    if out.get("auth_header"):
        out["auth_header"] = _REDACTED
    if isinstance(out.get("extra"), dict):
        out["extra"] = {k: _REDACTED for k in out["extra"]}
    if "upstream_url" in out:
        out["upstream_url"] = _redact_url(out["upstream_url"])
    if isinstance(out.get("tools"), dict):
        out["tools"] = _public_config(out["tools"])
        if "base_url" in out["tools"]:
            out["tools"]["base_url"] = _redact_url(out["tools"]["base_url"])
    return out


def _row_to_response(row: asyncpg.Record) -> AgentResponse:
    oasf_raw = row["oasf_skills"]
    if isinstance(oasf_raw, str):
        oasf_raw = json.loads(oasf_raw)
    oasf_skills = [OASFSkill.model_validate(s) for s in (oasf_raw or [])]

    config_raw = row["config"]
    if isinstance(config_raw, str):
        config_raw = json.loads(config_raw)

    return AgentResponse(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        handler_type=row["handler_type"],
        oasf_skills=oasf_skills,
        config=_public_config(config_raw),
        port=row["port"],
        status=row["status"],
        approved=row["approved"],
        has_handler=bool(row["handler_file_path"]),
        is_archive=bool(row.get("is_archive", False)),
        endpoint_url=row["endpoint_url"],
        ads_cid=row["ads_cid"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    port = int(os.getenv("REGISTRATION_SERVICE_PORT", "8091"))
    host = os.getenv("REGISTRATION_SERVICE_HOST", "0.0.0.0")
    uvicorn.run("registration_service.server:app", host=host, port=port, reload=False)
