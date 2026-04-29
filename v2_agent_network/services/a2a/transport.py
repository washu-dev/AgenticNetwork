from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

import httpx
from a2a.client import A2AClient as SdkA2AClient

from v2_agent_network.services.a2a.adapters import (
    a2a_response_to_internal_task_result,
    discovered_agent_to_a2a_agent_card,
    internal_task_request_to_a2a_request,
)
from v2_agent_network.services.common.models import A2ATaskRequest, A2ATaskResult, DiscoveredAgent

logger = logging.getLogger(__name__)


class A2ATransport(ABC):
    @abstractmethod
    async def send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        raise NotImplementedError


class HTTPA2ATransport(A2ATransport):
    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        http_clients_by_origin: dict[str, httpx.AsyncClient] | None = None,
    ):
        self._http_client = http_client
        self._http_clients_by_origin = http_clients_by_origin or {}

    async def send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        card = discovered_agent_to_a2a_agent_card(agent)
        sdk_request = internal_task_request_to_a2a_request(request)
        injected_client = self._client_for(card.url)
        logger.info(
            "a2a_http_send_start agent_id=%s endpoint=%s skill=%s task_id=%s context_id=%s injected_client=%s",
            agent.agent_id,
            card.url,
            request.skill,
            request.task_id,
            request.context_id,
            injected_client is not None,
        )
        if injected_client is not None:
            sdk_client = SdkA2AClient(httpx_client=injected_client, agent_card=card)
            sdk_response = await sdk_client.send_message(sdk_request)
        else:
            async with httpx.AsyncClient(timeout=30) as http_client:
                sdk_client = SdkA2AClient(httpx_client=http_client, agent_card=card)
                sdk_response = await sdk_client.send_message(sdk_request)
        result = a2a_response_to_internal_task_result(sdk_response)
        logger.info(
            "a2a_http_send_done agent_id=%s endpoint=%s skill=%s task_id=%s state=%s artifacts=%d error=%s",
            agent.agent_id,
            card.url,
            request.skill,
            result.task_id,
            result.state.value,
            len(result.artifacts),
            result.error,
        )
        return result

    def _client_for(self, url: str) -> httpx.AsyncClient | None:
        if self._http_client is not None:
            return self._http_client
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        for key in (url.rstrip("/"), origin):
            if key in self._http_clients_by_origin:
                return self._http_clients_by_origin[key]
        return None


class SlimA2ATransport(A2ATransport):
    async def send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        raise NotImplementedError("SLIM A2A transport is reserved for Milestone 5")


class NatsA2ATransport(A2ATransport):
    async def send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        raise NotImplementedError("NATS A2A transport is reserved for Milestone 5")
