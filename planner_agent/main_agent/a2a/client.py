from __future__ import annotations

from planner_agent.main_agent.a2a.transport import A2ATransport, HTTPA2ATransport, ProgressCallback
from planner_agent.main_agent.models import A2ATaskRequest, A2ATaskResult, DiscoveredAgent


class A2AClient:
    def __init__(self, transport: A2ATransport | None = None):
        self.transport = transport or HTTPA2ATransport()

    async def send_task(
        self,
        agent: DiscoveredAgent,
        request: A2ATaskRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> A2ATaskResult:
        return await self.transport.send_task(agent, request, progress_callback=progress_callback)
