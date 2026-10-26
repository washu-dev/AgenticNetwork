from __future__ import annotations

import logging
import os
import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from a2a.client import A2AClient as SdkA2AClient
from a2a.client import A2AClientHTTPError, A2AClientTimeoutError
from a2a.types import (
    Artifact,
    Message,
    SendStreamingMessageRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)

from agentnet.a2a.adapters import (
    a2a_response_to_internal_task_result,
    discovered_agent_to_a2a_agent_card,
    internal_task_request_to_a2a_request,
)
from agentnet.models import A2ATaskRequest, A2ATaskResult, DiscoveredAgent

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
    """Calls agents over A2A HTTP (JSON-RPC), streaming when the card says so.

    The clients it creates time out after ``timeout_seconds`` and raise
    httpx.HTTPStatusError for a non-2xx reply (``raise_for_error_status``). An injected
    client keeps its own settings: its timeout bounds streaming calls too, and it needs
    ``raise_for_error_status`` among its response hooks for a refused streaming call to
    report its own status.
    """

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
            sdk_response = await self._send(
                sdk_client, injected_client.timeout, sdk_request, card, agent, progress_callback
            )
        else:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                auth=CloudRunIdTokenAuth(),
                event_hooks={"response": [raise_for_error_status]},
            ) as http_client:
                sdk_client = SdkA2AClient(httpx_client=http_client, agent_card=card)
                sdk_response = await self._send(
                    sdk_client, http_client.timeout, sdk_request, card, agent, progress_callback
                )
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
        timeout: httpx.Timeout,
        sdk_request: Any,
        card: Any,
        agent: DiscoveredAgent,
        progress_callback: ProgressCallback | None,
    ) -> Any:
        """Use SSE streaming when the peer supports it; otherwise fall back to non-streaming.

        Streaming keeps bytes flowing while the peer thinks (each ``update_status``
        event from the executor reaches us as an SSE chunk), so the underlying
        httpx read timeout never fires on long-running peers — without raising
        the timeout itself. ``timeout`` is the http client's own, passed explicitly
        to the streaming call (see ``_stream``).
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
        async for envelope in _stream(sdk_client, streaming_request, timeout):
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


async def raise_for_error_status(response: httpx.Response) -> None:
    """httpx response hook: raise httpx.HTTPStatusError for a non-2xx reply.

    a2a-sdk 0.3.2's streaming call checks the reply's content type before its status, so
    without this an error page (Cloud Run's 403 for a caller it does not admit, an agent's
    500) is reported as HTTP 400 "Invalid SSE response". Non-streaming calls raise for
    the status themselves, with the same result.
    """
    if not response.is_success:
        response.raise_for_status()


async def _stream(
    sdk_client: SdkA2AClient,
    request: SendStreamingMessageRequest,
    timeout: httpx.Timeout,
) -> AsyncIterator[Any]:
    """``send_message_streaming`` bounded by ``timeout``, with its failures mapped to the
    SDK's client errors.

    a2a-sdk 0.3.2 streams with no timeout at all unless one is passed, so the http
    client's own timeout goes in explicitly: a peer that sends nothing for longer than
    its read timeout, before or between events, raises A2AClientTimeoutError, whichever
    SDK version wraps (or does not wrap) the underlying httpx timeout. The timeout bounds
    silence, not the whole call: a live peer's SSE pings or heartbeats keep it open.
    A non-2xx reply caught by ``raise_for_error_status`` raises A2AClientHTTPError with
    its status. Any other network error, such as a refused connection, raises
    A2AClientHTTPError 503 "Network communication error", as on the non-streaming path.
    One difference is left: a connect, write or pool timeout raises A2AClientTimeoutError
    here, while the SDK's non-streaming call reports it as that 503.
    """
    try:
        async for envelope in sdk_client.send_message_streaming(
            request, http_kwargs={"timeout": timeout}
        ):
            yield envelope
    except httpx.TimeoutException as exc:
        raise A2AClientTimeoutError("Client Request timed out") from exc
    except httpx.RequestError as exc:  # after TimeoutException, which is one
        raise A2AClientHTTPError(503, f"Network communication error: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise A2AClientHTTPError(exc.response.status_code, str(exc)) from exc
    except A2AClientHTTPError as exc:
        if isinstance(exc.__cause__, httpx.TimeoutException):
            raise A2AClientTimeoutError("Client Request timed out") from exc.__cause__
        raise


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
