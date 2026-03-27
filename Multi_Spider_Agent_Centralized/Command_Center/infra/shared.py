"""
Shared transport singleton for A2A communication.

Creates and manages a single transport instance (NATS or SLIM)
used by the A2A client to send messages to Pi agents.
"""

import logging
from typing import Optional

from ..config import DEFAULT_MESSAGE_TRANSPORT, TRANSPORT_SERVER_ENDPOINT
from .transport import BaseTransport, create_transport

logger = logging.getLogger(__name__)

_transport: Optional[BaseTransport] = None


def get_transport() -> BaseTransport:
    """Get or create the global transport singleton (not yet connected).

    Call `await get_transport().connect()` before first use.
    """
    global _transport
    if _transport is None:
        _transport = create_transport(
            DEFAULT_MESSAGE_TRANSPORT,
            endpoint=TRANSPORT_SERVER_ENDPOINT,
        )
    return _transport


def set_transport(transport: BaseTransport):
    """Override the global transport (for testing or custom config)."""
    global _transport
    _transport = transport


async def ensure_connected() -> BaseTransport:
    """Get transport, connecting if needed."""
    transport = get_transport()
    if not transport.is_connected:
        await transport.connect()
    return transport
