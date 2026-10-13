"""No-op supervisor used when FAKE_SUPERVISOR is on (default: the value of FAKE_DB).

Skips subprocess spawning and ADS publishing; updates the in-memory DB to
reflect what the real supervisor would do so callers see consistent status.
"""

from __future__ import annotations

import logging
import uuid

from registration_service.db import get_pool
from registration_service.port_pool import release_port

logger = logging.getLogger(__name__)


class FakeSupervisor:
    """Simulates spawn/stop without starting any process or touching ADS."""

    async def spawn_and_publish(self, agent_id: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agents WHERE id = $1", uuid.UUID(agent_id)
            )
        if row is None:
            raise ValueError(f"Agent {agent_id} not found")

        port: int = row["port"]
        endpoint_url = f"http://localhost:{port}/"

        from registration_service.supervisor import _set_status
        await _set_status(agent_id, "live", endpoint_url=endpoint_url, ads_cid="fake-cid")
        logger.info("fake_supervisor spawn agent_id=%s port=%d", agent_id, port)

    async def stop_and_unpublish(self, agent_id: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ads_cid, port FROM agents WHERE id = $1", uuid.UUID(agent_id)
            )
        if row is None:
            return

        port: int | None = row["port"]
        async with pool.acquire() as conn:
            if port:
                await release_port(conn, port)
            await conn.execute(
                "UPDATE agents SET status = 'stopped', ads_cid = NULL, updated_at = now() "
                "WHERE id = $1",
                uuid.UUID(agent_id),
            )
        logger.info("fake_supervisor stop agent_id=%s", agent_id)

    async def recover_live_agents(self) -> None:
        pass

    async def start_health_loop(self) -> None:
        pass

    async def stop_health_loop(self) -> None:
        pass

    async def start_build_poll_loop(self) -> None:
        pass

    async def stop_build_poll_loop(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def delete_github_context(self, name: str) -> None:
        pass

    async def spawn_queue(self) -> list:
        return []

    async def finalize_spawned(self, agent_id: str, pid: int) -> None:
        pass

    def stop_queue(self) -> list:
        return []
