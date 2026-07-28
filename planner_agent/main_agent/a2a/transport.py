from __future__ import annotations

import logging
import os
import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
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

from planner_agent.main_agent.a2a.adapters import (
    a2a_response_to_internal_task_result,
    discovered_agent_to_a2a_agent_card,
    internal_task_request_to_a2a_request,
)
from planner_agent.main_agent.models import A2ATaskRequest, A2ATaskResult, DiscoveredAgent

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

# Cached Google OIDC identity tokens, keyed by audience → (token, monotonic-expiry).
_RUN_ID_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _run_id_token(audience: str) -> str | None:
    """Mint (and cache ~50 min) a Google-signed OIDC identity token for a Cloud Run
    audience, using the runtime service account's ADC. Returns None if unavailable."""
    import time

    now = time.monotonic()
    cached = _RUN_ID_TOKEN_CACHE.get(audience)
    if cached and cached[1] > now:
        return cached[0]
    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token as google_id_token
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("a2a_run_auth_google_import_failed err=%s", exc)
        return None
    try:
        token = google_id_token.fetch_id_token(
            google.auth.transport.requests.Request(), audience
        )
    except Exception as exc:
        logger.warning("a2a_run_auth_token_fetch_failed audience=%s err=%s", audience, exc)
        return None
    _RUN_ID_TOKEN_CACHE[audience] = (token, now + 3000)
    return token


class CloudRunIdTokenAuth(httpx.Auth):
    """Attach a Google OIDC identity token when calling a private Cloud Run
    (``*.run.app``) agent, so the request passes Cloud Run's IAM invoker check.

    A no-op for every other host (localhost, LAN cluster, non-GCP endpoints), so the
    transport stays platform-agnostic — GCP auth is applied only where it's needed.
    """

    def auth_flow(self, request):
        host = request.url.host or ""
        if host.endswith(".run.app"):
            audience = f"{request.url.scheme}://{host}"
            token = _run_id_token(audience)
            if token:
                request.headers["Authorization"] = f"Bearer {token}"
        yield request


class A2ATransport(ABC):
    @abstractmethod
    async def send_task(
        self,
        agent: DiscoveredAgent,
        request: A2ATaskRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> A2ATaskResult:
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

    async def send_task(
        self,
        agent: DiscoveredAgent,
        request: A2ATaskRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> A2ATaskResult:
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
            sdk_response = await self._send(sdk_client, sdk_request, card, agent, progress_callback)
        else:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, auth=CloudRunIdTokenAuth()
            ) as http_client:
                sdk_client = SdkA2AClient(httpx_client=http_client, agent_card=card)
                sdk_response = await self._send(sdk_client, sdk_request, card, agent, progress_callback)
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

    async def _send(
        self,
        sdk_client: SdkA2AClient,
        sdk_request: Any,
        card: Any,
        agent: DiscoveredAgent,
        progress_callback: ProgressCallback | None,
    ) -> Any:
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
                if progress_callback is not None:
                    await _emit_progress(progress_callback, _progress_from_status_event(agent, event))
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
    async def send_task(
        self,
        agent: DiscoveredAgent,
        request: A2ATaskRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> A2ATaskResult:
        raise NotImplementedError("SLIM A2A transport is reserved for Milestone 5")


class NatsA2ATransport(A2ATransport):
    async def send_task(
        self,
        agent: DiscoveredAgent,
        request: A2ATaskRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> A2ATaskResult:
        raise NotImplementedError("NATS A2A transport is reserved for Milestone 5")


async def _emit_progress(callback: ProgressCallback, entry: dict[str, Any]) -> None:
    result = callback(entry)
    if inspect.isawaitable(result):
        await result


def _progress_from_status_event(agent: DiscoveredAgent, event: TaskStatusUpdateEvent) -> dict[str, Any]:
    message = event.status.message
    metadata = getattr(message, "metadata", None) if message is not None else None
    progress_event = metadata.get("progress_event") if isinstance(metadata, dict) else None
    content = dict(progress_event) if isinstance(progress_event, dict) else {}
    text = _message_text(message)
    if text:
        content.setdefault("text", text)
    content.update(
        {
            "agent_id": agent.agent_id,
            "agent_name": agent.name,
            "task_id": event.task_id,
            "context_id": event.context_id,
            "state": str(getattr(event.status.state, "value", event.status.state)),
            "final": bool(event.final),
        }
    )
    return {
        "tool": "remote_status",
        "status": content["state"],
        "content": content,
    }


def _message_text(message: Message | None) -> str:
    if message is None:
        return ""
    lines: list[str] = []
    for part in message.parts or []:
        text = getattr(part.root, "text", None)
        if text:
            lines.append(str(text))
    return "\n".join(lines).strip()
