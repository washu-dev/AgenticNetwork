"""
A2A Client: Send messages to Pi agents via NATS/SLIM transport.

Uses our lightweight transport abstraction (nats-py / slim-bindings directly),
no agntcy-app-sdk dependency. Falls back to HTTP POST when transport unavailable.
"""

import asyncio
import json
import logging
from typing import Optional
from uuid import uuid4

import httpx

from ..config import (
    DEFAULT_MESSAGE_TRANSPORT,
    A2A_TIMEOUT_SECONDS,
)
from .transport import agent_topic, broadcast_topic
from .shared import ensure_connected

logger = logging.getLogger(__name__)


def _build_a2a_request(prompt: str, metadata: Optional[dict] = None) -> dict:
    """Build a JSON-RPC A2A message/send payload."""
    msg_id = str(uuid4())
    message = {
        "messageId": msg_id,
        "role": "user",
        "parts": [{"kind": "text", "text": prompt}],
    }
    if metadata:
        message["metadata"] = metadata

    return {
        "jsonrpc": "2.0",
        "method": "message/send",
        "id": msg_id,
        "params": {"message": message},
    }


def _parse_a2a_response(data: dict) -> str:
    """Extract text from A2A JSON-RPC response."""
    result = data.get("result", {})
    parts = result.get("parts", [])
    text_parts = [p["text"] for p in parts if p.get("kind") == "text"]
    return "\n".join(text_parts) if text_parts else str(result)


class A2AClient:
    """Send A2A messages to Pi agents via NATS/SLIM or HTTP fallback."""

    async def send_task(
        self,
        robot_id: str,
        prompt: str,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Send an A2A message to a Pi agent via transport.

        Args:
            robot_id: Target robot ID (used to derive topic)
            prompt: Message text to send
            metadata: Optional metadata dict (task_type, action, params, etc.)

        Returns:
            {"status": "success", "response": "...", "robot_id": "..."} or error dict
        """
        msg_id = str(uuid4())
        topic = agent_topic(robot_id, DEFAULT_MESSAGE_TRANSPORT)

        try:
            transport = await ensure_connected()

            # Build A2A JSON-RPC payload
            payload = _build_a2a_request(prompt, metadata)
            payload_bytes = json.dumps(payload).encode()

            # Send via transport and wait for reply
            response_bytes = await transport.request(
                topic, payload_bytes, timeout=A2A_TIMEOUT_SECONDS
            )

            # Parse response
            data = json.loads(response_bytes)
            response_text = _parse_a2a_response(data)

            return {
                "status": "success",
                "robot_id": robot_id,
                "message_id": msg_id,
                "response": response_text,
                "raw": data,
            }

        except Exception as e:
            logger.error(f"A2A transport send failed to {robot_id}: {e}")
            return {
                "status": "error",
                "robot_id": robot_id,
                "message_id": msg_id,
                "error": str(e),
            }

    async def send_task_http(
        self,
        host: str,
        port: int,
        prompt: str,
        metadata: Optional[dict] = None,
    ) -> dict:
        """HTTP fallback: send A2A JSON-RPC directly (no transport layer).

        Used when NATS/SLIM transport isn't available.
        """
        url = f"http://{host}:{port}/"
        msg_id = str(uuid4())
        payload = _build_a2a_request(prompt, metadata)

        try:
            async with httpx.AsyncClient(timeout=A2A_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()

            response_text = _parse_a2a_response(data)

            return {
                "status": "success",
                "host": host,
                "port": port,
                "message_id": msg_id,
                "response": response_text,
                "raw": data,
            }
        except Exception as e:
            logger.error(f"A2A HTTP send failed to {host}:{port}: {e}")
            return {
                "status": "error",
                "host": host,
                "port": port,
                "message_id": msg_id,
                "error": str(e),
            }

    async def send_task_to_multiple(
        self,
        robot_ids: list[str],
        prompt: str,
        metadata: Optional[dict] = None,
    ) -> list[dict]:
        """Send the same task to multiple agents in parallel via transport."""
        tasks = [
            self.send_task(rid, prompt, metadata)
            for rid in robot_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed.append({
                    "status": "error",
                    "robot_id": robot_ids[i] if i < len(robot_ids) else "unknown",
                    "error": str(result),
                })
            else:
                processed.append(result)
        return processed

    async def broadcast_notification(
        self,
        notification: dict,
        exclude_robot_id: str = None,
    ):
        """Broadcast a notification to all robots.

        Primary: publish to NATS broadcast topic (fire-and-forget).
        Fallback: send individually to each online robot via transport.

        Args:
            notification: Notification metadata (notification_type, finder_id, etc.)
            exclude_robot_id: Robot ID to exclude (e.g. the finder itself).
        """
        prompt = notification.get("text", "Notification from Central")
        metadata = {**notification, "task_type": "notification"}
        payload = _build_a2a_request(prompt, metadata)
        payload_bytes = json.dumps(payload).encode()

        try:
            transport = await ensure_connected()

            # NATS: fire-and-forget publish to broadcast topic
            topic = broadcast_topic(DEFAULT_MESSAGE_TRANSPORT)
            await transport.publish(topic, payload_bytes)
            logger.info(f"Broadcast notification: {notification.get('notification_type')} via {topic}")

        except Exception as e:
            # Fallback: send individually to each online robot
            logger.warning(f"Broadcast publish failed ({e}), falling back to individual sends")
            try:
                from .robot_registry import robot_registry
                online = robot_registry.get_online_robots()
                robot_ids = [
                    r.robot_id for r in online
                    if r.robot_id != exclude_robot_id
                ]
                if robot_ids:
                    await self.send_task_to_multiple(robot_ids, prompt, metadata)
            except Exception as fallback_err:
                logger.error(f"Broadcast fallback also failed: {fallback_err}")
