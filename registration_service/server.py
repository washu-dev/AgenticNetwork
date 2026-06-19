"""FastAPI server — REST API for the registration service."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from registration_service.db import get_pool
from registration_service.lifespan import lifespan
from registration_service.models import AgentResponse, OASFSkill, RegisterRequest
from registration_service.oasf_skills import OASF_SKILLS
from registration_service.port_pool import assign_port
from registration_service.supervisor import get_supervisor

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="AgenticNetwork Registration Service",
    description="Register LLM agents and API proxies into the AgenticNetwork.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Web UI ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=FileResponse, include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


# ── OASF skill list ────────────────────────────────────────────────────────────

@app.get("/oasf-skills", tags=["reference"])
async def list_oasf_skills() -> list[dict]:
    """Return the full flat OASF skill taxonomy available for registration."""
    return OASF_SKILLS


# ── Agent CRUD ─────────────────────────────────────────────────────────────────

@app.post("/agents", response_model=AgentResponse, status_code=201, tags=["agents"])
async def register_agent(req: RegisterRequest) -> AgentResponse:
    """Register a new agent and start it."""
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
                req.handler_type,
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

            row = await conn.fetchrow("SELECT * FROM agents WHERE id = $1", uuid.UUID(agent_id))

    try:
        await get_supervisor().spawn_and_publish(agent_id)
    except Exception as exc:
        logger.error("register_agent_spawn_failed agent_id=%s error=%s", agent_id, exc)
        raise HTTPException(status_code=500, detail=f"Agent registered but failed to start: {exc}")

    pool = await get_pool()
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM agents WHERE id = $1", _parse_uuid(agent_id)
        )
    if not exists:
        raise HTTPException(status_code=404, detail="Agent not found")

    await get_supervisor().stop_and_unpublish(agent_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agents WHERE id = $1", _parse_uuid(agent_id))
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
        endpoint_url=row["endpoint_url"],
        ads_cid=row["ads_cid"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    import os

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    port = int(os.getenv("REGISTRATION_SERVICE_PORT", "8091"))
    uvicorn.run("registration_service.server:app", host="0.0.0.0", port=port, reload=False)
