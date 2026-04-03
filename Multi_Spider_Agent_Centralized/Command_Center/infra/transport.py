"""
Transport abstraction for A2A message delivery.

Replaces agntcy-app-sdk (which requires Python >= 3.12) with direct usage of:
  - nats-py (Python >= 3.7) for NATS pub/sub
  - slim-bindings (Python >= 3.10) for SLIM encrypted messaging

Both transports expose the same interface: request/reply for clients,
subscribe for servers.
"""

import asyncio
import datetime
import json
import logging
from abc import ABC, abstractmethod
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class BaseTransport(ABC):
    """Abstract transport for A2A message delivery."""

    @abstractmethod
    async def connect(self):
        """Establish connection to the transport broker."""

    @abstractmethod
    async def close(self):
        """Close the transport connection."""

    async def publish(self, topic: str, payload: bytes):
        """Fire-and-forget publish (no reply expected). Used for broadcasts.

        Default implementation is a no-op. Override in transports that support it.
        """
        logger.warning(f"publish() not supported on {self.__class__.__name__}")

    @abstractmethod
    async def request(self, topic: str, payload: bytes, timeout: float = 30) -> bytes:
        """Send a message and wait for a reply (client-side request/reply pattern).

        Args:
            topic: Target topic/identity (e.g. "agents.direct.crawler-001")
            payload: A2A JSON-RPC message bytes
            timeout: Max seconds to wait for reply

        Returns:
            Reply payload bytes
        """

    @abstractmethod
    async def subscribe(self, topics: list[str], handler: Callable):
        """Subscribe to topics and handle incoming messages (server-side).

        Handler signature: async def handler(payload: bytes, reply: Callable)
          - payload: incoming message bytes
          - reply: async callable to send response bytes back
        """

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether transport is currently connected."""


class NatsTransport(BaseTransport):
    """NATS pub/sub transport.

    Uses nats-py directly. Supports request/reply pattern natively.
    Requires: nats-py >= 2.6.0 (Python >= 3.7)

    Topic format: "agents.direct.{robot_id}", "agents.broadcast"
    """

    def __init__(self, endpoint: str = "nats://localhost:4222"):
        self._endpoint = endpoint
        self._nc = None
        self._subscriptions = []

    async def connect(self):
        import nats
        self._nc = await nats.connect(self._endpoint)
        logger.info(f"NATS connected: {self._endpoint}")

    async def close(self):
        if self._nc and self._nc.is_connected:
            for sub in self._subscriptions:
                await sub.unsubscribe()
            await self._nc.close()
            logger.info("NATS disconnected")

    async def publish(self, topic: str, payload: bytes):
        """Fire-and-forget publish (no reply expected). Used for broadcasts."""
        if not self._nc or not self._nc.is_connected:
            raise ConnectionError("NATS not connected")
        await self._nc.publish(topic, payload)

    async def request(self, topic: str, payload: bytes, timeout: float = 30) -> bytes:
        if not self._nc or not self._nc.is_connected:
            raise ConnectionError("NATS not connected")
        response = await self._nc.request(topic, payload, timeout=timeout)
        return response.data

    async def subscribe(self, topics: list[str], handler: Callable):
        if not self._nc or not self._nc.is_connected:
            raise ConnectionError("NATS not connected")

        async def _nats_handler(msg):
            async def reply_fn(response_bytes: bytes):
                if msg.reply:
                    await self._nc.publish(msg.reply, response_bytes)

            await handler(msg.data, reply_fn)

        for topic in topics:
            sub = await self._nc.subscribe(topic, cb=_nats_handler)
            self._subscriptions.append(sub)
            logger.info(f"NATS subscribed: {topic}")

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected


class SlimTransport(BaseTransport):
    """SLIM encrypted transport.

    Uses slim-bindings directly. Persistent sessions between agents.
    Requires: slim-bindings (Python >= 3.10)

    Identity format: "default/default/{agent_name}"
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:46357",
        identity: str = "default/default/command_center",
        secret: str = "multi-spider-shared-secret",
    ):
        self._endpoint = endpoint
        self._identity = identity
        self._secret = secret
        self._service = None
        self._app = None
        self._conn_id = None
        self._sessions: dict[str, object] = {}  # topic -> session
        self._listen_task: Optional[asyncio.Task] = None

    async def connect(self):
        import slim_bindings

        slim_bindings.initialize_crypto_provider()
        self._service = slim_bindings.get_global_service()

        name = slim_bindings.Name.from_string(self._identity)
        self._app = self._service.create_app_with_secret(name, self._secret)

        client_config = slim_bindings.new_insecure_client_config(self._endpoint)
        self._conn_id = await self._service.connect_async(client_config)

        # Subscribe so we can receive messages
        name_with_id = slim_bindings.Name.new_with_id(
            *name.components(), id=self._app.id()
        )
        await self._app.subscribe_async(name_with_id, self._conn_id)

        logger.info(f"SLIM connected: {self._endpoint} as {self._identity}")

    async def close(self):
        if self._app:
            for session in self._sessions.values():
                try:
                    await self._app.delete_session_async(session)
                except Exception:
                    pass
            self._sessions.clear()
        if self._listen_task:
            self._listen_task.cancel()
        logger.info("SLIM disconnected")

    async def request(self, topic: str, payload: bytes, timeout: float = 30) -> bytes:
        import slim_bindings

        if not self._app:
            raise ConnectionError("SLIM not connected")

        # Get or create persistent session to target
        if topic not in self._sessions:
            remote = slim_bindings.Name.from_string(topic)
            await self._app.set_route_async(remote, self._conn_id)

            config = slim_bindings.SessionConfig(
                session_type=slim_bindings.SessionType.POINT_TO_POINT,
                enable_mls=False,
                max_retries=5,
                interval=datetime.timedelta(seconds=5),
                metadata={},
            )
            ctx = await self._app.create_session_async(config, remote)
            await ctx.completion.wait_async()
            self._sessions[topic] = ctx.session

        session = self._sessions[topic]

        # Send request and wait for reply
        await session.publish_and_wait_async(payload, "application/json", None)
        reply = await session.get_message_async(
            timeout=datetime.timedelta(seconds=timeout)
        )
        return reply.payload

    async def subscribe(self, topics: list[str], handler: Callable):
        """Listen for incoming SLIM sessions and handle messages.

        For SLIM, topics are informational — messages arrive via sessions,
        not topic subscriptions. We listen for all incoming sessions.
        """
        if not self._app:
            raise ConnectionError("SLIM not connected")

        async def _listen_loop():
            while True:
                try:
                    session = await self._app.listen_for_session_async(
                        timeout=datetime.timedelta(seconds=60)
                    )
                    asyncio.create_task(_handle_session(session))
                except Exception as e:
                    if "timeout" in str(e).lower():
                        continue
                    logger.error(f"SLIM listen error: {e}")
                    await asyncio.sleep(1)

        async def _handle_session(session):
            while True:
                try:
                    msg = await session.get_message_async(
                        timeout=datetime.timedelta(seconds=60)
                    )

                    async def reply_fn(response_bytes: bytes):
                        await session.publish_to_async(
                            msg.context, response_bytes, "application/json", None
                        )

                    await handler(msg.payload, reply_fn)
                except Exception as e:
                    if "closed" in str(e).lower() or "timeout" in str(e).lower():
                        break
                    logger.error(f"SLIM session error: {e}")

        self._listen_task = asyncio.create_task(_listen_loop())
        logger.info(f"SLIM listening for sessions (topics: {topics})")

    @property
    def is_connected(self) -> bool:
        return self._app is not None


def create_transport(transport_type: str, **kwargs) -> BaseTransport:
    """Factory function to create a transport by type.

    Args:
        transport_type: "NATS", "SLIM", or "HTTP"
        **kwargs: Transport-specific config
            NATS: endpoint (str)
            SLIM: endpoint (str), identity (str), secret (str)

    Returns:
        BaseTransport instance (not yet connected — call await transport.connect())
    """
    t = transport_type.upper()
    if t == "NATS":
        return NatsTransport(endpoint=kwargs.get("endpoint", "nats://localhost:4222"))
    elif t == "SLIM":
        return SlimTransport(
            endpoint=kwargs.get("endpoint", "http://localhost:46357"),
            identity=kwargs.get("identity", "default/default/command_center"),
            secret=kwargs.get("secret", "multi-spider-shared-secret"),
        )
    else:
        raise ValueError(f"Unknown transport type: {transport_type}. Use 'NATS' or 'SLIM'.")
