"""
UDP Video Frame Receiver.

Receives JPEG frames pushed by Pi's stream.py via UDP.
Protocol: each datagram = header(8 bytes) + chunk data
  Header: frame_id(4B) + chunk_index(2B) + num_chunks(2B)

Multiple chunks are reassembled into complete JPEG frames.
"""

import asyncio
import logging
import socket
import struct
import time
from typing import Optional

logger = logging.getLogger(__name__)


class UDPFrameReceiver:
    """Receives UDP video frames from Pi and reassembles them."""

    def __init__(self, listen_port: int = 0):
        """
        Args:
            listen_port: UDP port to listen on. 0 = auto-assign.
        """
        self._sock: socket.socket = None
        self._port = listen_port
        self._running = False
        self._latest_frame: Optional[bytes] = None
        self._frame_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

        # Reassembly buffer: frame_id -> {chunks: {index: data}, expected: int, timestamp: float}
        self._assembly: dict = {}

    @property
    def port(self) -> int:
        """Actual port (useful when auto-assigned with port=0)."""
        return self._port

    @property
    def latest_frame(self) -> Optional[bytes]:
        return self._latest_frame

    def start(self):
        """Start receiving UDP frames in background."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self._sock.bind(("0.0.0.0", self._port))
        self._sock.setblocking(False)
        self._port = self._sock.getsockname()[1]
        self._running = True
        self._task = asyncio.get_event_loop().create_task(self._receive_loop())
        logger.info(f"UDP receiver started on port {self._port}")

    def stop(self):
        """Stop receiving."""
        self._running = False
        if self._task:
            self._task.cancel()
        if self._sock:
            self._sock.close()
        logger.info("UDP receiver stopped")

    async def wait_for_frame(self, timeout: float = 5.0) -> Optional[bytes]:
        """Wait for next complete frame. Returns JPEG bytes or None on timeout."""
        self._frame_event.clear()
        try:
            await asyncio.wait_for(self._frame_event.wait(), timeout=timeout)
            return self._latest_frame
        except asyncio.TimeoutError:
            return None

    async def _receive_loop(self):
        """Background loop: receive UDP datagrams and reassemble frames."""
        loop = asyncio.get_event_loop()

        while self._running:
            try:
                data = await loop.sock_recv(self._sock, 65535)
                if len(data) < 8:
                    continue

                # Parse header
                frame_id, chunk_idx, num_chunks = struct.unpack("!IHH", data[:8])
                chunk_data = data[8:]

                # Single-chunk frame (common for small frames)
                if num_chunks == 1:
                    self._latest_frame = chunk_data
                    self._frame_event.set()
                    continue

                # Multi-chunk: reassemble
                if frame_id not in self._assembly:
                    self._assembly[frame_id] = {
                        "chunks": {},
                        "expected": num_chunks,
                        "timestamp": time.time(),
                    }

                entry = self._assembly[frame_id]
                entry["chunks"][chunk_idx] = chunk_data

                # Complete?
                if len(entry["chunks"]) == entry["expected"]:
                    # Reassemble in order
                    frame_data = b""
                    for i in range(entry["expected"]):
                        frame_data += entry["chunks"].get(i, b"")
                    self._latest_frame = frame_data
                    self._frame_event.set()
                    del self._assembly[frame_id]

                # Cleanup stale partial frames (> 2 seconds old)
                now = time.time()
                stale = [fid for fid, e in self._assembly.items() if now - e["timestamp"] > 2]
                for fid in stale:
                    del self._assembly[fid]

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.debug(f"UDP receive error: {e}")
                    await asyncio.sleep(0.01)
