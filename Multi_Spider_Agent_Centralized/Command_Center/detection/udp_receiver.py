"""
UDP Video Frame Receiver.

Receives JPEG frames pushed by Pi's stream.py via UDP.
Protocol: each datagram = header(8 bytes) + chunk data
  Header: frame_id(4B) + chunk_index(2B) + num_chunks(2B)

Multiple chunks are reassembled into complete JPEG frames.
Uses asyncio DatagramProtocol for reliable async UDP receive.
"""

import asyncio
import logging
import struct
import time
from typing import Optional

logger = logging.getLogger(__name__)


class _UDPProtocol(asyncio.DatagramProtocol):
    """Asyncio protocol that reassembles UDP chunks into JPEG frames."""

    def __init__(self, receiver: "UDPFrameReceiver"):
        self._receiver = receiver
        self._assembly: dict = {}

    def datagram_received(self, data: bytes, addr):
        if len(data) < 8:
            return

        frame_id, chunk_idx, num_chunks = struct.unpack("!IHH", data[:8])
        chunk_data = data[8:]

        # Single-chunk frame
        if num_chunks == 1:
            self._receiver._on_frame(chunk_data)
            return

        # Multi-chunk: reassemble
        if frame_id not in self._assembly:
            self._assembly[frame_id] = {
                "chunks": {},
                "expected": num_chunks,
                "timestamp": time.time(),
            }

        entry = self._assembly[frame_id]
        entry["chunks"][chunk_idx] = chunk_data

        if len(entry["chunks"]) == entry["expected"]:
            frame_data = b"".join(entry["chunks"][i] for i in range(entry["expected"]))
            self._receiver._on_frame(frame_data)
            del self._assembly[frame_id]

        # Cleanup stale partial frames
        now = time.time()
        stale = [fid for fid, e in self._assembly.items() if now - e["timestamp"] > 2]
        for fid in stale:
            del self._assembly[fid]

    def error_received(self, exc):
        logger.debug(f"UDP error: {exc}")

    def connection_lost(self, exc):
        pass


class UDPFrameReceiver:
    """Receives UDP video frames from Pi and reassembles them."""

    def __init__(self, listen_port: int = 0):
        self._port = listen_port
        self._running = False
        self._latest_frame: Optional[bytes] = None
        self._frame_event = asyncio.Event()
        self._transport = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def latest_frame(self) -> Optional[bytes]:
        return self._latest_frame

    def _on_frame(self, frame_data: bytes):
        """Called by protocol when a complete frame is received."""
        self._latest_frame = frame_data
        self._frame_event.set()

    async def start(self):
        """Start receiving UDP frames."""
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDPProtocol(self),
            local_addr=("0.0.0.0", self._port),
        )
        self._transport = transport
        self._port = transport.get_extra_info("sockname")[1]
        self._running = True
        logger.info(f"UDP receiver started on port {self._port}")

    def stop(self):
        """Stop receiving."""
        self._running = False
        if self._transport:
            self._transport.close()
        logger.info("UDP receiver stopped")

    async def wait_for_frame(self, timeout: float = 5.0) -> Optional[bytes]:
        """Wait for next complete frame. Returns JPEG bytes or None on timeout."""
        self._frame_event.clear()
        try:
            await asyncio.wait_for(self._frame_event.wait(), timeout=timeout)
            return self._latest_frame
        except asyncio.TimeoutError:
            return None
