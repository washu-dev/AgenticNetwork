from __future__ import annotations

from abc import ABC, abstractmethod

import httpx
from a2a.client import A2AClient as SdkA2AClient

from v2_agent_network.services.a2a.adapters import (
    a2a_response_to_internal_task_result,
    discovered_agent_to_a2a_agent_card,
    internal_task_request_to_a2a_request,
)
from v2_agent_network.services.common.models import A2ATaskRequest, A2ATaskResult, DiscoveredAgent


class A2ATransport(ABC):
    @abstractmethod
    async def send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        raise NotImplementedError


class HTTPA2ATransport(A2ATransport):
    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._http_client = http_client

    async def send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        card = discovered_agent_to_a2a_agent_card(agent)
        sdk_request = internal_task_request_to_a2a_request(request)
        if self._http_client is not None:
            sdk_client = SdkA2AClient(httpx_client=self._http_client, agent_card=card)
            sdk_response = await sdk_client.send_message(sdk_request)
        else:
            async with httpx.AsyncClient(timeout=30) as http_client:
                sdk_client = SdkA2AClient(httpx_client=http_client, agent_card=card)
                sdk_response = await sdk_client.send_message(sdk_request)
        return a2a_response_to_internal_task_result(sdk_response)


class SlimA2ATransport(A2ATransport):
    async def send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        raise NotImplementedError("SLIM A2A transport is reserved for Milestone 5")


class NatsA2ATransport(A2ATransport):
    async def send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        raise NotImplementedError("NATS A2A transport is reserved for Milestone 5")

