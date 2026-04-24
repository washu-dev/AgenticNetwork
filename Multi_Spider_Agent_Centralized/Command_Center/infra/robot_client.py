"""
Robot Client: sends commands to Physical Pi via HTTP POST /command.

Direct TCP/IP communication — no NATS, no A2A protocol.
Robot Agent calls this to control the physical robot.
"""

import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class RobotClient:
    """Send commands to Physical Pi via HTTP POST /command.

    Each command is a JSON payload with task_type and parameters.
    Pi's thin_client routes these to the appropriate hardware handler.
    """

    async def execute_action(
        self, host: str, port: int, action: str, params: dict = None,
        robot_id: str = "unknown",
    ) -> dict:
        """Execute a hardware action on the robot."""
        payload = {
            "task_type": "execute",
            "action": action,
            "params": params or {},
        }
        result = await self._post_command(host, port, payload)
        if result.get("status") == "error":
            logger.error(f"Command failed: {action} on {robot_id}: {result.get('error')}")
        return result

    async def read_ultrasonic(self, host: str, port: int, robot_id: str = "unknown") -> Optional[float]:
        """Read ultrasonic distance sensor. Returns distance in cm or None."""
        result = await self._post_command(host, port, {"task_type": "read_sensor", "sensor": "ultrasonic"})
        return result.get("distance_cm")

    async def get_status(self, host: str, port: int, robot_id: str = "unknown") -> dict:
        """Get robot status."""
        return await self._post_command(host, port, {"task_type": "status"})

    async def speak(self, host: str, port: int, text: str, robot_id: str = "unknown") -> dict:
        """Send TTS command to robot."""
        return await self._post_command(host, port, {"task_type": "speak", "text": text})

    async def send_notification(self, host: str, port: int, notification: dict, robot_id: str = "unknown") -> dict:
        """Send notification to robot (e.g. target_found → sit_down)."""
        payload = {**notification, "task_type": "notification"}
        return await self._post_command(host, port, payload)

    async def health_check(self, host: str, port: int) -> Optional[dict]:
        """Check robot health via GET /health."""
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"http://{host}:{port}/health")
                resp.raise_for_status()
                return resp.json()
        except Exception:
            return None

    async def start_video_stream(self, host: str, camera_port: int, target_host: str, target_port: int, fps: int = 10) -> dict:
        """Tell Pi to start pushing UDP video frames to target address."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(
                    f"http://{host}:{camera_port}/stream/start",
                    json={"target_host": target_host, "target_port": target_port, "fps": fps},
                )
                return resp.json()
        except Exception as e:
            logger.warning(f"Failed to start video stream: {e}")
            return {"status": "error", "error": str(e)}

    async def stop_video_stream(self, host: str, camera_port: int) -> dict:
        """Tell Pi to stop pushing UDP video frames."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(f"http://{host}:{camera_port}/stream/stop")
                return resp.json()
        except Exception as e:
            logger.warning(f"Failed to stop video stream: {e}")
            return {"status": "error", "error": str(e)}

    async def trigger_video_playback(self, host: str, camera_port: int) -> Optional[float]:
        """Mock-only: tell mock_pi to play its recorded video from frame 0.

        Real Pi camera never has a /stream/play endpoint — it returns 404
        and we ignore it.  Mock Pi honours it by unfreezing its video for
        one full play-through and reports the clip duration back.

        Returns:
            The video's duration in seconds when the endpoint exists and
            responds with ``playback_seconds`` (mock Pi path).
            ``None`` for real Pi / unreachable / response without the key.
        """
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                r = await client.post(f"http://{host}:{camera_port}/stream/play")
                if r.status_code == 200:
                    data = r.json()
                    secs = data.get("playback_seconds")
                    if isinstance(secs, (int, float)) and secs > 0:
                        return float(secs)
        except Exception:
            # Real Pi / unreachable / not a mock — silently ignore.
            pass
        return None

    async def pause_video_playback(self, host: str, camera_port: int) -> None:
        """Mock-only: tell mock_pi to pause its recorded video and rewind
        to frame 0.  Called on mission cancel so the next task starts from
        a clean opener instead of mid-playback.

        Real Pi has no /stream/pause endpoint — 404 is silently ignored.
        """
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                await client.post(f"http://{host}:{camera_port}/stream/pause")
        except Exception:
            pass

    async def _post_command(self, host: str, port: int, payload: dict) -> dict:
        """POST a command to Pi's /command endpoint."""
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(f"http://{host}:{port}/command", json=payload)
                return resp.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}


# Singleton
robot_client = RobotClient()
