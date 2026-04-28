from __future__ import annotations

from v2_agent_network.services.a2a.transport import A2ATransport, HTTPA2ATransport
from v2_agent_network.services.common.models import A2ATaskRequest, A2ATaskResult, DiscoveredAgent


class A2AClient:
    def __init__(self, transport: A2ATransport | None = None):
        self.transport = transport or HTTPA2ATransport()

    async def send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        return await self.transport.send_task(agent, request)

