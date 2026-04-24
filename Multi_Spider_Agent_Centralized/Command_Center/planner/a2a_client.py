"""A2A client helper for the Planner — multi-transport edition.

Uses ``agntcy_app_sdk.semantic.a2a.client.A2AClientFactory`` to negotiate
the right transport (HTTP / NATS patterns / SLIM patterns) for every
Robot Agent card.  The transport is picked by the client factory from
the intersection of:

  - the card's ``preferred_transport`` + ``additional_interfaces``
  - the client's ``ClientConfig`` (which transports it can speak)

We configure the planner to speak **all three** so any robot can be
reached regardless of how it was started.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, Client
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    TaskIdParams,
    TextPart,
)
from agntcy_app_sdk.semantic.a2a.client.config import (
    ClientConfig,
    NatsTransportConfig,
    SlimTransportConfig,
)
from agntcy_app_sdk.semantic.a2a.client.factory import A2AClientFactory

# ── NATS patterns request/reply timeout patch ───────────────────────────
# agntcy_app_sdk's NatsTransport.request() hard-codes a 60 s reply
# timeout (see transport/nats/transport.py).  The PatternsClientTransport
# that wraps it for A2A does NOT pass through a timeout override, so
# every A2A-over-NATS request waits at most 60 s.  Mock missions (and
# real Pi patrols) can run 3-10 minutes, so we globally raise the default
# by monkey-patching the class method at import time.  No effect on the
# HTTP/JSONRPC path.
try:  # pragma: no cover — env-dependent import guard
    from agntcy_app_sdk.transport.nats.transport import NatsTransport as _NatsTransport
    _orig_nats_request = _NatsTransport.request

    async def _patched_nats_request(self, recipient, message, timeout: int = 600, **kwargs):
        return await _orig_nats_request(self, recipient, message, timeout=timeout, **kwargs)

    _NatsTransport.request = _patched_nats_request
except Exception:
    pass

from ..config import (
    DEFAULT_MESSAGE_TRANSPORT,
    NATS_ENDPOINT,
    SLIM_ENDPOINT,
    SLIM_SHARED_SECRET,
    A2A_ENABLE_HTTP,
    A2A_ENABLE_NATS,
    A2A_ENABLE_SLIM,
)

logger = logging.getLogger(__name__)


class RobotA2AError(Exception):
    """Raised when A2A communication with a Robot Agent fails."""


class RobotA2AClient:
    """Per-robot A2A client cache with transport auto-negotiation.

    One ``A2AClientFactory`` is shared across all robots.  The factory's
    ``ClientConfig`` advertises every transport the planner can speak;
    the factory then negotiates the best match against each robot's
    ``AgentCard``.
    """

    def __init__(self, request_timeout: float = 180.0):
        self._httpx: Optional[httpx.AsyncClient] = None
        self._request_timeout = request_timeout
        self._client_factory: Optional[A2AClientFactory] = None
        self._clients: dict[str, Client] = {}       # base_url -> Client
        self._cards: dict[str, AgentCard] = {}       # base_url -> AgentCard
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_ready(self) -> A2AClientFactory:
        if self._client_factory is not None:
            return self._client_factory

        self._httpx = httpx.AsyncClient(timeout=self._request_timeout)

        kwargs: dict[str, Any] = {"httpx_client": self._httpx, "streaming": False, "polling": False}

        # Planner can speak NATS only if NATS transport is enabled in our
        # deployment.  Same for SLIM.  JSONRPC is always supported via the
        # httpx client.  (These flags control what the client *can* do —
        # the actual negotiated transport is still picked from the card.)
        if A2A_ENABLE_NATS:
            kwargs["nats_config"] = NatsTransportConfig(endpoint=NATS_ENDPOINT)
        if A2A_ENABLE_SLIM:
            kwargs["slim_config"] = SlimTransportConfig(
                endpoint=SLIM_ENDPOINT,
                name=f"default/default/planner-{uuid4().hex[:8]}",
                shared_secret_identity=SLIM_SHARED_SECRET,
            )

        config = ClientConfig(**kwargs)
        self._client_factory = A2AClientFactory(config)
        logger.info(
            f"A2A client factory ready — supported_transports="
            f"{config.supported_transports} (preferred remote={DEFAULT_MESSAGE_TRANSPORT})"
        )
        return self._client_factory

    async def close(self) -> None:
        if self._httpx is not None:
            try:
                await self._httpx.aclose()
            except Exception:
                pass
            self._httpx = None
        self._clients.clear()
        self._cards.clear()
        self._client_factory = None

    # ------------------------------------------------------------------
    # Card / client acquisition
    # ------------------------------------------------------------------

    async def get_card(self, base_url: str) -> AgentCard:
        """Fetch (and cache) the AgentCard at ``base_url`` via HTTP.

        ``base_url`` is the Robot Agent's HTTP endpoint
        (``http://host:port``).  This is always available in our
        deployment because A2A_ENABLE_HTTP defaults to True.
        """
        if base_url in self._cards:
            return self._cards[base_url]
        if self._httpx is None:
            self._httpx = httpx.AsyncClient(timeout=self._request_timeout)
        resolver = A2ACardResolver(self._httpx, base_url=base_url)
        card = await resolver.get_agent_card()
        if not card.url:
            card.url = base_url
        self._cards[base_url] = card
        return card

    async def _get_client(self, base_url: str) -> Client:
        async with self._lock:
            cli = self._clients.get(base_url)
            if cli is not None:
                return cli
            factory = await self._ensure_ready()
            card = await self.get_card(base_url)
            cli = await factory.create(card)
            self._clients[base_url] = cli
            chosen = getattr(card, "preferred_transport", "?")
            logger.info(f"A2A client created for {base_url} (remote preferred_transport={chosen})")
            return cli

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def ping(self, base_url: str, timeout: float = 3.0) -> bool:
        """Cheap liveness check — fetches the AgentCard over HTTP.

        Note: this requires ``A2A_ENABLE_HTTP=True`` on the Robot Agent
        side (which is our default).  If someone runs a NATS-only robot,
        ping still works because we can always resolve the card from ADS
        instead.  For the simple per-URL path here, HTTP is assumed.
        """
        try:
            if self._httpx is None:
                self._httpx = httpx.AsyncClient(timeout=timeout)
            resp = await self._httpx.get(
                f"{base_url.rstrip('/')}/.well-known/agent-card.json",
                timeout=timeout,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"A2A ping failed for {base_url}: {e}")
            return False

    async def send_task(
        self,
        base_url: str,
        task_payload: dict,
        prompt: str = "",
        timeout: float = 180.0,
    ) -> dict:
        """Send a structured task to a Robot Agent.

        Works transparently over whichever transport the card negotiates —
        HTTP, NATS patterns, or SLIM patterns.
        """
        try:
            client = await self._get_client(base_url)
        except Exception as e:
            raise RobotA2AError(f"Cannot build A2A client for {base_url}: {e}") from e

        text = prompt or task_payload.get("task_goal") or task_payload.get("prompt") or ""
        if not text:
            text = f"Execute task_type={task_payload.get('task_type', 'unspecified')}"

        message = Message(
            message_id=str(uuid4()),
            role=Role.user,
            parts=[Part(TextPart(text=text))],
            metadata={"robot_task": task_payload},
        )

        try:
            final = await asyncio.wait_for(
                self._collect_final_response(client, message),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise RobotA2AError(f"A2A send_task timed out after {timeout}s ({base_url})")
        except Exception as e:
            raise RobotA2AError(f"A2A send_task failed ({base_url}): {e}") from e

        return self._extract_result(final)

    async def cancel_task(self, base_url: str, task_id: str) -> dict:
        """Request the remote executor to cancel the given task id."""
        try:
            client = await self._get_client(base_url)
            task = await client.cancel_task(TaskIdParams(id=task_id))
            return {"status": "cancelled", "task_id": task_id, "task_status": str(task.status)}
        except Exception as e:
            raise RobotA2AError(f"A2A cancel_task failed ({base_url}): {e}") from e

    # ------------------------------------------------------------------
    # Response handling
    # ------------------------------------------------------------------

    @staticmethod
    async def _collect_final_response(client: Client, message: Message) -> Any:
        last: Any = None
        async for item in client.send_message(message):
            last = item
        return last

    @staticmethod
    def _extract_result(final: Any) -> dict:
        """Turn the final stream item into the legacy ``{status, ...}`` dict."""
        # Case 1: tuple (Task, event_or_None)
        if isinstance(final, tuple) and len(final) == 2:
            task, _ = final
            history = getattr(task, "history", None) or []
            for msg in reversed(history):
                meta = getattr(msg, "metadata", None) or {}
                if "robot_task_result" in meta:
                    return meta["robot_task_result"]
            status = getattr(task, "status", None)
            status_msg = getattr(status, "message", None) if status else None
            meta = getattr(status_msg, "metadata", None) or {}
            if "robot_task_result" in meta:
                return meta["robot_task_result"]
            return {
                "status": "success",
                "task_id": getattr(task, "id", None),
                "task_state": str(getattr(status, "state", "?")) if status else "?",
            }

        # Case 2: direct Message reply
        meta = getattr(final, "metadata", None) or {}
        if "robot_task_result" in meta:
            return meta["robot_task_result"]

        # Case 3: text fallback
        parts = getattr(final, "parts", None) or []
        for part in parts:
            text = getattr(getattr(part, "root", part), "text", None)
            if text:
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    return {"status": "success", "response": text}

        return {"status": "error", "error": "Empty A2A response"}


# ---------------------------------------------------------------------------
# Module-level singleton so planner graph nodes can share one client.
# ---------------------------------------------------------------------------

_client: Optional[RobotA2AClient] = None


def get_a2a_client() -> RobotA2AClient:
    global _client
    if _client is None:
        _client = RobotA2AClient()
    return _client


async def close_a2a_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
