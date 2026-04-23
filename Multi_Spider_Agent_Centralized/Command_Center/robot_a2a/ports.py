"""Deterministic A2A HTTP port assignment per robot.

Each Robot Agent needs its own HTTP port for the A2A Starlette
application.  We hash robot_id to a stable offset so the port is
consistent across restarts without requiring a central allocator.
"""

import hashlib
import os

A2A_HTTP_PORT_BASE = int(os.getenv("A2A_HTTP_PORT_BASE", "9100"))
A2A_HTTP_PORT_RANGE = int(os.getenv("A2A_HTTP_PORT_RANGE", "100"))


def a2a_http_port(robot_id: str) -> int:
    """Deterministic A2A HTTP port for a given robot_id."""
    h = int(hashlib.md5(robot_id.encode()).hexdigest()[:8], 16)
    return A2A_HTTP_PORT_BASE + (h % A2A_HTTP_PORT_RANGE)
