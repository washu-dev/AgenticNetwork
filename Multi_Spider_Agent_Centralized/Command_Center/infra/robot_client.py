"""
Robot Client: sends commands to Pi robots via A2A protocol.

Uses NATS/SLIM transport with HTTP fallback.
The Pi's thin-client A2A executor handles command routing.
"""

import json
import logging
from typing import Optional

import httpx

from .a2a_client import A2AClient

logger = logging.getLogger(__name__)


class RobotClient:
    """Send commands to Pi robots via A2A protocol.

    Each command is sent as an A2A message with metadata specifying
    the task_type and parameters. The Pi's A2A executor routes these
    to the appropriate hardware handler.

    Tries transport (NATS/SLIM) first, falls back to HTTP on failure.
    """

    def __init__(self):
        self._a2a = A2AClient()

    async def execute_action(
        self, host: str, port: int, action: str, params: dict = None,
        robot_id: str = "unknown",
    ) -> dict:
        """Execute a hardware action on the robot via A2A."""
        metadata = {
            "task_type": "execute",
            "action": action,
            "params": params or {},
        }

        result = await self._send_with_fallback(
            robot_id, host, port, f"Execute {action}", metadata
        )

        if result.get("status") == "error":
            logger.error(f"Command failed: {action} on {host}:{port}: {result.get('error')}")
            return {"status": "error", "error": result.get("error", "A2A send failed")}

        return self._parse_json_response(result, fallback={"status": "ok", "action": action})

    async def read_ultrasonic(self, host: str, port: int, robot_id: str = "unknown") -> Optional[float]:
        """Read ultrasonic distance sensor via A2A. Returns distance in cm or None."""
        metadata = {"task_type": "read_sensor", "sensor": "ultrasonic"}

        result = await self._send_with_fallback(
            robot_id, host, port, "Read ultrasonic", metadata
        )

        if result.get("status") == "error":
            logger.debug(f"Ultrasonic read failed from {host}:{port}: {result.get('error')}")
            return None

        parsed = self._parse_json_response(result)
        if parsed:
            return parsed.get("distance_cm")
        return None

    async def get_status(self, host: str, port: int, robot_id: str = "unknown") -> dict:
        """Get robot status via A2A."""
        metadata = {"task_type": "status"}

        result = await self._send_with_fallback(
            robot_id, host, port, "Get status", metadata
        )

        if result.get("status") == "error":
            logger.debug(f"Status check failed for {host}:{port}: {result.get('error')}")
            return {"status": "error", "error": result.get("error", "")}

        return self._parse_json_response(result, fallback={"status": "unknown"})

    async def speak(self, host: str, port: int, text: str, robot_id: str = "unknown") -> dict:
        """Send TTS command to robot via A2A."""
        metadata = {"task_type": "speak", "text": text}

        result = await self._send_with_fallback(
            robot_id, host, port, f"Speak: {text}", metadata
        )

        if result.get("status") == "error":
            logger.debug(f"Speak failed on {host}:{port}: {result.get('error')}")
            return {"status": "error", "error": result.get("error", "")}

        return self._parse_json_response(result, fallback={"status": "ok"})

    async def health_check(self, host: str, port: int) -> Optional[dict]:
        """Check robot health via HTTP GET /health (always direct HTTP)."""
        url = f"http://{host}:{port}/health"
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
        except Exception:
            return None

    async def _send_with_fallback(
        self, robot_id: str, host: str, port: int, prompt: str, metadata: dict
    ) -> dict:
        """Try transport first, fall back to HTTP on failure."""
        try:
            result = await self._a2a.send_task(robot_id, prompt, metadata)
            if result.get("status") != "error":
                logger.info(f"Sent via transport to {robot_id}")
                return result
            logger.warning(f"Transport returned error for {robot_id}: {result.get('error')}, falling back to HTTP")
        except Exception as e:
            logger.warning(f"Transport exception for {robot_id}: {e}, falling back to HTTP")

        return await self._a2a.send_task_http(host, port, prompt, metadata)

    @staticmethod
    def _parse_json_response(a2a_result: dict, fallback: dict = None) -> dict:
        """Parse JSON from A2A response text."""
        response_text = a2a_result.get("response", "")
        try:
            return json.loads(response_text)
        except (json.JSONDecodeError, TypeError):
            pass

        structured = a2a_result.get("structured", {})
        if structured:
            return structured

        return fallback or {}


# Singleton
robot_client = RobotClient()
