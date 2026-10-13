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

import asyncpg
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ads_utils.publisher import DirectoryPublisher
from registration_service import db
from registration_service.archive import ArchiveError, _clear_dir, list_tree, unpack_archive, zip_tree
from registration_service.backend import _handler_files_dir, _image_slug
from registration_service.db import get_pool
from registration_service.lifespan import lifespan
from registration_service.models import (
    AgentResponse,
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
from registration_service.supervisor import _deserialize_config, get_supervisor
from registration_service.validator import validate_handler, validate_tool_module


def _uploads_code(agent: dict) -> bool:
    """Whether a stored agent ships uploaded Python — the gate for the whole
    register-unapproved → upload → approve lifecycle and the handler
    upload/review/download/approve/reject endpoints below.

    Asks the CONFIG, not the handler_type name: both a ``custom`` handler and an
    ``llm`` agent whose ``tools.source`` is ``upload`` qualify. A malformed stored
    config is treated as "not uploaded code" so these endpoints 404 rather than 500.
    """
    try:
        return carries_upload(_deserialize_config(agent))
    except Exception:
        return False


def _handler_dir(agent_id: Any) -> Path:
    return _handler_files_dir() / str(agent_id)


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

    The planner proxies the approver's requests through, forwarding the Apache-injected
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


# /ready probe timeouts (the planner's values). dirctl is a subprocess, so the
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
    """Readiness, in the planner's shape: 200 when no check is in error, else 503.

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
    # three (e.g. two "Echo Test" agents share echo-test). Reject up front.
    new_slug = _image_slug(req.name)
    async with (await get_pool()).acquire() as conn:
        existing = await conn.fetch("SELECT * FROM agents ORDER BY created_at DESC")
    if any(_image_slug(r["name"]) == new_slug for r in existing):
        raise HTTPException(
            status_code=409,
            detail=f"An agent with a conflicting name already exists (slug '{new_slug}'): {req.name!r}",
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

    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = str(uuid.uuid4())
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
async def delete_agent(agent_id: str) -> Response:
    """Stop and unregister an agent."""
    agent = await db.get_agent(_parse_uuid(agent_id))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    await get_supervisor().stop_and_unpublish(agent_id)
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
    """Manually restart an agent."""
    pool = await get_pool()
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

    The entry file is AST-validated (structural errors block with 422; advisory
    warnings are returned and shown to the approver). Re-upload always resets
    approval to False.
    """
    agent = await db.get_agent(_parse_uuid(agent_id))
    if not agent or not _uploads_code(agent):
        raise HTTPException(status_code=404, detail="Uploaded-code agent not found")

    config = _deserialize_config(agent)
    entry_file = upload_spec(config).entry_file   # non-None: _uploads_code() passed

    raw = await file.read()
    is_zip = _is_zip_upload(file.filename, raw)
    handler_dir = _handler_dir(agent_id)

    if is_zip:
        try:
            unpack_archive(raw, handler_dir, entry_file)
        except ArchiveError as exc:
            raise HTTPException(status_code=422, detail={"errors": [str(exc)]})
        entry_path = handler_dir / entry_file
        source = entry_path.read_text(encoding="utf-8", errors="replace")
    else:
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=422, detail={"errors": ["File is not valid UTF-8 text"]})
        handler_dir.mkdir(parents=True, exist_ok=True)
        entry_path = handler_dir / entry_file
        entry_path.write_text(source)

    # Validate the entry file only (structural blocks; blocklist is advisory — D1).
    # An uploaded TOOL module checks the tool contract (TOOL_SCHEMAS/TOOL_REGISTRY or
    # build_tools); a custom handler checks the AgentHandler subclass. Which one is
    # decided by the config, not the handler_type name.
    if isinstance(config, CustomConfig):
        result = validate_handler(source, config.class_name)
    else:
        result = validate_tool_module(source)
    if not result.ok:
        if is_zip:
            _clear_dir(handler_dir)  # don't leave a rejected tree on disk
        raise HTTPException(status_code=422, detail={"errors": result.errors})

    await db.update_agent(
        _parse_uuid(agent_id),
        handler_file_path=str(entry_path),
        is_archive=is_zip,
        approved=False,
    )
    return {"status": "uploaded", "errors": [], "warnings": result.warnings}


@app.get("/agents/{agent_id}/handler", tags=["custom"])
async def get_handler_source(agent_id: str, request: Request) -> dict:
    """Admin-only: what the approver reviews before approving (the trust boundary).

    - single-file agent → the entry-file ``source`` (as before).
    - multi-file agent  → a ``manifest`` of the stored tree + a ``download_url``
      pointing at the zip (GET .../handler/download), so the whole project can be
      inspected. In both cases advisory ``warnings`` from the entry file are included.
    """
    _require_admin(request)
    agent = await db.get_agent(_parse_uuid(agent_id))
    if not agent or not _uploads_code(agent):
        raise HTTPException(status_code=404, detail="Uploaded-code agent not found")
    path = agent.get("handler_file_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="No handler file uploaded")

    entry_source = Path(path).read_text(encoding="utf-8", errors="replace")
    config = _deserialize_config(agent)
    spec = upload_spec(config)                    # non-None: _uploads_code() passed
    if isinstance(config, CustomConfig):
        warnings = validate_handler(entry_source, config.class_name).warnings
        credentials = [c.model_dump() for c in config.credentials]   # user creds it asks for (P4a)
    else:
        warnings = validate_tool_module(entry_source).warnings
        credentials: list = []   # uploaded tool modules declare no per-user creds (v1)

    if agent.get("is_archive"):
        handler_dir = _handler_dir(agent_id)
        return {
            "is_archive": True,
            "entry_file": Path(path).name,
            "manifest": list_tree(handler_dir),
            "download_url": f"/agents/{agent_id}/handler/download",
            "warnings": warnings,
            "system_packages": spec.system_packages,   # apt, flagged for review (D10)
            "credentials": credentials,
        }
    return {
        "is_archive": False,
        "source": entry_source,
        "handler_file_path": path,
        "warnings": warnings,
        "system_packages": spec.system_packages,         # apt, flagged for review (D10)
        "credentials": credentials,
    }


@app.get("/agents/{agent_id}/handler/download", tags=["custom"])
async def download_handler_archive(agent_id: str, request: Request) -> Response:
    """Admin-only: download the stored project tree as a zip (multi-file inspection)."""
    _require_admin(request)
    agent = await db.get_agent(_parse_uuid(agent_id))
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
async def approve_agent(agent_id: str, request: Request) -> dict:
    """Admin-only: approve a custom agent and start it."""
    _require_admin(request)
    agent = await db.get_agent(_parse_uuid(agent_id))
    if not agent or not _uploads_code(agent):
        raise HTTPException(status_code=404, detail="Uploaded-code agent not found")
    if not agent.get("handler_file_path"):
        raise HTTPException(status_code=409, detail="No handler file uploaded yet")

    await db.update_agent(_parse_uuid(agent_id), approved=True)
    try:
        await get_supervisor().spawn_and_publish(agent_id)
    except Exception as exc:
        logger.error("approve_spawn_failed agent_id=%s error=%s", agent_id, exc)
        raise HTTPException(status_code=500, detail=f"Approved but failed to start: {exc}")
    return {"status": "ok"}


@app.post("/agents/{agent_id}/reject", tags=["custom"])
async def reject_agent(agent_id: str, body: RejectRequest, request: Request) -> dict:
    """Admin-only: reject a custom agent (leaves it registered, not approved)."""
    _require_admin(request)
    agent = await db.get_agent(_parse_uuid(agent_id))
    if not agent or not _uploads_code(agent):
        raise HTTPException(status_code=404, detail="Uploaded-code agent not found")

    await db.update_agent(_parse_uuid(agent_id), approved=False, status="registered")
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
        config=config_raw,
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
