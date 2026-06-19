"""API proxy handler — forwards incoming A2A tasks to an upstream HTTP service."""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)

_PROXY_TIMEOUT_SECONDS = 120.0


class APIProxyExecutor(AgentExecutor):
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
            async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    self._upstream_url,
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                result = resp.json()
        except Exception as exc:
            logger.exception("api_proxy_failed agent_id=%s upstream=%s", self._agent_id, self._upstream_url)
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
        capabilities=AgentCapabilities(streaming=False),
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
    executor = APIProxyExecutor(agent_id, upstream_url, auth_header)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(
        agent_card=card,
        http_handler=request_handler,
    ).build()
    logger.info("api_proxy_starting agent_id=%s port=%d upstream=%s", agent_id, port, upstream_url)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
