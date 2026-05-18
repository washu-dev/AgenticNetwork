from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlsplit

import httpx
from a2a.client import A2AClient as SdkA2AClient
from a2a.types import (
    Artifact,
    Message,
    SendStreamingMessageRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)

from _vendor_v2.a2a.adapters import (
    a2a_response_to_internal_task_result,
    discovered_agent_to_a2a_agent_card,
    internal_task_request_to_a2a_request,
)
from _vendor_v2.common.models import A2ATaskRequest, A2ATaskResult, DiscoveredAgent

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
        timeout_seconds: float | None = None,
    ):
        self._http_client = http_client
        self._http_clients_by_origin = http_clients_by_origin or {}
        self.timeout_seconds = timeout_seconds or float(os.getenv("A2A_HTTP_TIMEOUT_SECONDS", "30"))

    async def send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        card = discovered_agent_to_a2a_agent_card(agent)
        sdk_request = internal_task_request_to_a2a_request(request)
        injected_client = self._client_for(card.url)
        logger.info(
            "a2a_http_send_start agent_id=%s endpoint=%s skill=%s task_id=%s context_id=%s injected_client=%s streaming=%s",
            agent.agent_id,
            card.url,
            request.skill,
            request.task_id,
            request.context_id,
            injected_client is not None,
            card.capabilities.streaming if card.capabilities else False,
        )
        if injected_client is not None:
            sdk_client = SdkA2AClient(httpx_client=injected_client, agent_card=card)
            sdk_response = await self._send(sdk_client, sdk_request, card)
        else:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as http_client:
                sdk_client = SdkA2AClient(httpx_client=http_client, agent_card=card)
                sdk_response = await self._send(sdk_client, sdk_request, card)
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

    async def _send(self, sdk_client: SdkA2AClient, sdk_request: Any, card: Any) -> Any:
        """Use SSE streaming when the peer supports it; otherwise fall back to non-streaming.

        Streaming keeps bytes flowing while the peer thinks (each ``update_status``
        event from the executor reaches us as an SSE chunk), so the underlying
        httpx read timeout never fires on long-running peers — without raising
        the timeout itself.
        """
        if not (card.capabilities and card.capabilities.streaming):
            return await sdk_client.send_message(sdk_request)
        streaming_request = SendStreamingMessageRequest(
            id=sdk_request.id,
            params=sdk_request.params,
        )
        artifacts_by_id: dict[str, Artifact] = {}
        last_task: Task | None = None
        last_message: Message | None = None
        async for envelope in sdk_client.send_message_streaming(streaming_request):
            event = envelope.root.result
            if isinstance(event, Task):
                last_task = event
                for art in event.artifacts or []:
                    artifacts_by_id[art.artifact_id] = art
            elif isinstance(event, Message):
                last_message = event
            elif isinstance(event, TaskStatusUpdateEvent):
                logger.info(
                    "a2a_stream status task_id=%s state=%s final=%s",
                    event.task_id, event.status.state, event.final,
                )
                if last_task is None:
                    last_task = Task(
                        id=event.task_id,
                        context_id=event.context_id,
                        status=event.status,
                        kind="task",
                    )
                else:
                    last_task = last_task.model_copy(update={"status": event.status})
                if event.final:
                    break
            elif isinstance(event, TaskArtifactUpdateEvent):
                key = event.artifact.artifact_id
                if event.append and key in artifacts_by_id:
                    existing = artifacts_by_id[key]
                    artifacts_by_id[key] = existing.model_copy(
                        update={"parts": list(existing.parts) + list(event.artifact.parts)}
                    )
                else:
                    artifacts_by_id[key] = event.artifact
        if last_task is not None:
            return last_task.model_copy(update={"artifacts": list(artifacts_by_id.values())})
        if last_message is not None:
            return last_message
        raise RuntimeError("a2a stream ended without a terminal event")

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
