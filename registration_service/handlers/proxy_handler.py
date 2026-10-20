"""Proxy handler — A2A server for the ``proxy`` agent type (legacy name ``api_proxy``).

No LLM: each incoming A2A task is forwarded to an upstream HTTP/JSON-RPC service
the author hosts, and the upstream's reply is returned as the answer.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
import uvicorn
from a2a.server.agent_execution import AgentExecutor
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    DataPart,
    Part,
    TextPart,
)

from registration_service.handlers.heartbeat import await_with_heartbeat

logger = logging.getLogger(__name__)

_PROXY_TIMEOUT_SECONDS = 120.0


class ProxyExecutor(AgentExecutor):
    """Forwards an A2A task to an upstream URL and returns its response."""

    def __init__(self, agent_id: str, upstream_url: str, auth_header: str | None) -> None:
        self._agent_id = agent_id
        self._upstream_url = upstream_url.rstrip("/")
        self._auth_header = auth_header

    async def execute(self, context: Any, event_queue: Any) -> None:
        from a2a.server.tasks import TaskUpdater

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()

        user_text = _get_user_text(context)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_header:
            headers["Authorization"] = self._auth_header

        payload = {
            "jsonrpc": "2.0",
            "id": context.task_id,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": user_text}],
                    "messageId": context.task_id,
                }
            },
        }

        try:
            # The upstream may take up to _PROXY_TIMEOUT_SECONDS; heartbeats keep
            # a streaming caller connected meanwhile.
            result = await await_with_heartbeat(updater, self._forward(payload, headers))
        except Exception as exc:
            logger.exception("proxy_failed agent_id=%s upstream=%s", self._agent_id, self._upstream_url)
            await updater.complete(
                message=updater.new_agent_message(
                    [Part(root=TextPart(text=f"Upstream request failed: {exc}"))]
                )
            )
            return

        reply_text = _extract_text(result)
        output = {
            "agent_id": self._agent_id,
            "upstream_url": self._upstream_url,
            "upstream_response": result,
        }
        await updater.add_artifact(
            [Part(root=DataPart(data=output))],
            artifact_id=f"{self._agent_id}-proxy-response",
            name="proxy_response",
        )
        await updater.complete(
            message=updater.new_agent_message([Part(root=TextPart(text=reply_text))])
        )

    async def _forward(self, payload: dict[str, Any], headers: dict[str, str]) -> Any:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT_SECONDS) as client:
            resp = await client.post(self._upstream_url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def cancel(self, context: Any, event_queue: Any) -> None:
        pass


def _get_user_text(context: Any) -> str:
    try:
        return str(context.get_user_input())
    except Exception:
        return ""


def _extract_text(result: Any) -> str:
    """Best-effort extraction of a human-readable string from an A2A JSON-RPC response."""
    if not isinstance(result, dict):
        return str(result)
    result_body = result.get("result", result)
    # Try common A2A response shapes
    for path in (
        ("status", "message", "parts"),
        ("artifact", "parts"),
        ("parts",),
    ):
        node: Any = result_body
        for key in path:
            if isinstance(node, dict):
                node = node.get(key)
            else:
                node = None
                break
        if isinstance(node, list):
            texts = [
                p.get("text", "") if isinstance(p, dict) else ""
                for p in node
            ]
            joined = " ".join(t for t in texts if t)
            if joined:
                return joined
    return json.dumps(result_body, indent=2)


def build_agent_card(agent_id: str, name: str, description: str, port: int) -> AgentCard:
    return AgentCard(
        name=name,
        description=description,
        url=f"http://localhost:{port}/",
        version="1.0.0",
        # Must match the published card (streaming=True); see llm_handler.
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id=f"{agent_id}/proxy",
                name="proxy",
                description=description,
                tags=[],
            )
        ],
        default_input_modes=["text"],
        default_output_modes=["text"],
    )


def run(
    agent_id: str,
    name: str,
    description: str,
    upstream_url: str,
    auth_header: str | None,
    port: int,
) -> None:
    card = build_agent_card(agent_id, name, description, port)
    executor = ProxyExecutor(agent_id, upstream_url, auth_header)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(
        agent_card=card,
        http_handler=request_handler,
    ).build()
    logger.info("proxy_starting agent_id=%s port=%d upstream=%s", agent_id, port, upstream_url)
    uvicorn.run(app, host=os.getenv("AGENT_BIND_HOST", "0.0.0.0"), port=port, log_level="warning")
