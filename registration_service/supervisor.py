"""Process supervisor — spawn, publish, monitor, and recover registered agents."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from ads_utils.converter import OfficialOASFSkill, _AGENT_OASF_ROUTING_SKILLS
from ads_utils.publisher import DirectoryPublisher, DirectoryUnavailable
from a2a.types import AgentCard, AgentCapabilities, AgentSkill

from registration_service.backend import SubprocessBackend
from registration_service.db import get_pool
from registration_service.models import LLMConfig, APIProxyConfig
from registration_service.port_pool import release_port

logger = logging.getLogger(__name__)

_HEALTH_POLL_SECONDS = 30.0
_BACKOFF_BASE_SECONDS = 5.0
_MAX_RESTART_ATTEMPTS = 3


class Supervisor:
    def __init__(
        self,
        backend: SubprocessBackend | None = None,
        publisher: DirectoryPublisher | None = None,
    ) -> None:
        self._backend = backend or SubprocessBackend()
        self._publisher = publisher or DirectoryPublisher()
        self._health_task: asyncio.Task[None] | None = None

    # ── Public API ─────────────────────────────────────────────────────────────

    async def spawn_and_publish(self, agent_id: str) -> None:
        """Spawn the agent subprocess, wait for health, then publish to ADS.

        Updates agent status to 'live' on success or 'failed' on error.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agents WHERE id = $1", uuid.UUID(agent_id)
            )
        if row is None:
            raise ValueError(f"Agent {agent_id} not found in DB")

        agent = dict(row)
        config = _deserialize_config(agent)
        port: int = agent["port"]
        name: str = agent["name"]
        description: str = agent["description"]
        handler_type: str = agent["handler_type"]
        raw_skills = agent["oasf_skills"]
        oasf_skills: list = raw_skills if isinstance(raw_skills, list) else json.loads(raw_skills)

        _register_oasf_routing_skills(name, oasf_skills)

        try:
            await self._backend.spawn(
                agent_id=agent_id,
                name=name,
                description=description,
                handler_type=handler_type,
                config=config,
                port=port,
            )
        except Exception as exc:
            logger.error("spawn_failed agent_id=%s error=%s", agent_id, exc)
            await _set_status(agent_id, "failed")
            raise

        endpoint_url = f"http://localhost:{port}/"
        card = _build_agent_card(agent_id, name, description, oasf_skills, port)

        ads_cid: str | None = None
        try:
            record = self._publisher.publish(card, advertise_url=endpoint_url)
            ads_cid = record.backend_ref
            logger.info("ads_published agent_id=%s cid=%s", agent_id, ads_cid)
        except DirectoryUnavailable as exc:
            logger.warning("ads_publish_skipped agent_id=%s reason=%s", agent_id, str(exc).replace("\n", " | "))

        await _set_status(agent_id, "live", endpoint_url=endpoint_url, ads_cid=ads_cid)

    async def stop_and_unpublish(self, agent_id: str) -> None:
        """Unpublish from ADS, stop the subprocess, release the port."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ads_cid, port FROM agents WHERE id = $1", uuid.UUID(agent_id)
            )
        if row is None:
            return

        ads_cid: str | None = row["ads_cid"]
        port: int | None = row["port"]

        if ads_cid:
            try:
                self._publisher.unpublish(ads_cid)
            except DirectoryUnavailable as exc:
                logger.warning("ads_unpublish_failed agent_id=%s error=%s", agent_id, exc)

        await self._backend.stop(agent_id)

        async with pool.acquire() as conn:
            if port:
                await release_port(conn, port)
            await conn.execute(
                "UPDATE agents SET status = 'stopped', ads_cid = NULL, updated_at = now() "
                "WHERE id = $1",
                uuid.UUID(agent_id),
            )
        logger.info("agent_stopped agent_id=%s", agent_id)

    async def restart_with_backoff(self, agent_id: str) -> None:
        """Attempt up to _MAX_RESTART_ATTEMPTS restarts with exponential backoff.

        Sets status='failed' if all attempts are exhausted.
        """
        for attempt in range(1, _MAX_RESTART_ATTEMPTS + 1):
            delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.info(
                "restart_attempt agent_id=%s attempt=%d/%d delay=%.1fs",
                agent_id,
                attempt,
                _MAX_RESTART_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)
            await self._backend.stop(agent_id)
            try:
                await self.spawn_and_publish(agent_id)
                logger.info("restart_succeeded agent_id=%s attempt=%d", agent_id, attempt)
                return
            except Exception as exc:
                logger.warning(
                    "restart_failed agent_id=%s attempt=%d error=%s", agent_id, attempt, exc
                )

        logger.error("restart_exhausted agent_id=%s", agent_id)
        await _set_status(agent_id, "failed")

    async def recover_live_agents(self) -> None:
        """On startup, re-spawn all agents that were 'live' before the last shutdown."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM agents WHERE status = 'live'"
            )
        agent_ids = [str(row["id"]) for row in rows]
        if not agent_ids:
            return
        logger.info("recovery_starting count=%d", len(agent_ids))
        results = await asyncio.gather(
            *[self.spawn_and_publish(aid) for aid in agent_ids],
            return_exceptions=True,
        )
        for aid, result in zip(agent_ids, results):
            if isinstance(result, Exception):
                logger.error("recovery_failed agent_id=%s error=%s", aid, result)

    # ── Health-check loop ──────────────────────────────────────────────────────

    async def start_health_loop(self) -> None:
        self._health_task = asyncio.create_task(self._health_loop(), name="supervisor-health")

    async def stop_health_loop(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEALTH_POLL_SECONDS)
            try:
                await self._check_all_live_agents()
            except Exception as exc:
                logger.exception("health_loop_error error=%s", exc)

    async def _check_all_live_agents(self) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM agents WHERE status = 'live'"
            )
        for row in rows:
            agent_id = str(row["id"])
            if not await self._backend.is_alive(agent_id):
                logger.warning("health_check_dead agent_id=%s triggering_restart", agent_id)
                asyncio.create_task(
                    self.restart_with_backoff(agent_id),
                    name=f"restart-{agent_id}",
                )


# ── Module-level singleton ─────────────────────────────────────────────────────

_supervisor: Supervisor | None = None


def get_supervisor() -> Supervisor:
    global _supervisor
    if _supervisor is None:
        import os
        if os.getenv("FAKE_DB"):
            from registration_service.fake_supervisor import FakeSupervisor
            _supervisor = FakeSupervisor()  # type: ignore[assignment]
        else:
            _supervisor = Supervisor()
    return _supervisor


def set_supervisor(sup: Any) -> None:
    """Override the supervisor singleton — used in tests."""
    global _supervisor
    _supervisor = sup


# ── Helpers ────────────────────────────────────────────────────────────────────

def _deserialize_config(agent: dict[str, Any]) -> LLMConfig | APIProxyConfig:
    handler_type: str = agent["handler_type"]
    raw = agent["config"]
    config_data: dict = raw if isinstance(raw, dict) else json.loads(raw)
    if handler_type == "llm":
        return LLMConfig.model_validate(config_data)
    return APIProxyConfig.model_validate(config_data)


def _build_agent_card(
    agent_id: str,
    name: str,
    description: str,
    oasf_skills: list[dict[str, Any]],
    port: int,
) -> AgentCard:
    skills = [
        AgentSkill(
            id=s["key"],
            name=s["name"].split("/")[-1],
            description=description,
            tags=[],
        )
        for s in oasf_skills
    ]
    return AgentCard(
        name=name,
        description=description,
        url=f"http://localhost:{port}/",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        skills=skills,
        default_input_modes=["text"],
        default_output_modes=["text"],
    )


def _register_oasf_routing_skills(
    name: str, oasf_skills: list[dict[str, Any]]
) -> None:
    """Add the agent's OASF skills to the converter's routing map so dirctl publish works."""
    agent_key = name.lower().replace(" ", "-")
    _AGENT_OASF_ROUTING_SKILLS[agent_key] = [
        OfficialOASFSkill(name=s["key"], id=s.get("id"))
        for s in oasf_skills
    ]


async def _set_status(
    agent_id: str,
    status: str,
    *,
    endpoint_url: str | None = None,
    ads_cid: str | None = None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agents
            SET status = $1,
                endpoint_url = COALESCE($2, endpoint_url),
                ads_cid = COALESCE($3, ads_cid),
                updated_at = now()
            WHERE id = $4
            """,
            status,
            endpoint_url,
            ads_cid,
            uuid.UUID(agent_id),
        )
