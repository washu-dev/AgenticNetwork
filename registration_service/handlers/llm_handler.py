"""LLM handler — A2A server for an ``llm`` agent whose ``tools.source`` is
``none``, ``presets`` or ``raw``, backed by the shared agent_skeleton engine.

The agent is routed through the spec engine (``agent_skeleton.llm_loop.run_agent``
+ ``spec.llm_wrapper_spec``) rather than a bespoke single-turn call. ``none`` is a
single LLM turn; ``presets`` adds tools chosen by name from
``agent_skeleton.system_tools.preset_tools``, which the loop lets the model call
before it answers; ``raw`` schemas have no bodies, so the runner serves those
agents prompt-only (as ``none``). Only the spec differs — the engine loop is shared
with every other ``llm`` source.

``LLMAgentExecutor`` and ``build_agent_card`` here are also used by
``llm_upload_handler`` (``upload``) and ``llm_http_handler`` (``http``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

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

from agent_skeleton.llm_loop import run_agent
from agent_skeleton.spec import AgentSpec, llm_wrapper_spec
from registration_service.handlers.heartbeat import await_with_heartbeat

logger = logging.getLogger(__name__)


class LLMAgentExecutor(AgentExecutor):
    """LLM executor backed by the shared tool-loop engine + a prebuilt AgentSpec."""

    def __init__(self, agent_id: str, spec: AgentSpec, model: str) -> None:
        self._agent_id = agent_id
        self._spec = spec
        self._model = model

    async def execute(self, context: Any, event_queue: Any) -> None:
        from a2a.server.tasks import TaskUpdater

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()

        user_text = _get_user_text(context)
        if not user_text.strip():
            await updater.complete(
                message=updater.new_agent_message(
                    [Part(root=TextPart(text="No input provided."))]
                )
            )
            return

        try:
            # run_agent is a blocking (sync OpenAI) tool loop; keep the loop free,
            # and keep a streaming caller's connection alive while it runs.
            result = await await_with_heartbeat(
                updater,
                asyncio.to_thread(
                    run_agent, {"prompt": user_text}, spec=self._spec, model=self._model
                ),
            )
        except Exception as exc:
            logger.exception("llm_handler_failed agent_id=%s", self._agent_id)
            await updater.complete(
                message=updater.new_agent_message(
                    [Part(root=TextPart(text=f"LLM call failed: {exc}"))]
                )
            )
            return

        reply = str(result.get("response_text") or result.get("answer") or "").strip()
        if not reply:
            reply = "(no answer produced)"
        # Dual channel: structured DataPart (full engine result) + human text.
        output = {"agent_id": self._agent_id, **result}
        await updater.add_artifact(
            [Part(root=DataPart(data=output))],
            artifact_id=f"{self._agent_id}-response",
            name="llm_response",
        )
        await updater.complete(
            message=updater.new_agent_message([Part(root=TextPart(text=reply))])
        )

    async def cancel(self, context: Any, event_queue: Any) -> None:
        pass


def _get_user_text(context: Any) -> str:
    try:
        return str(context.get_user_input())
    except Exception:
        return ""


def build_agent_card(agent_id: str, name: str, description: str, port: int) -> AgentCard:
    return AgentCard(
        name=name,
        description=description,
        url=f"http://localhost:{port}/",
        version="1.0.0",
        # Must match the card the supervisor publishes (streaming=True): the
        # planner picks message/stream from the published card, and the A2A SDK
        # rejects message/stream when the served card says False.
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id=f"{agent_id}/respond",
                name="respond",
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
    system_prompt: str,
    model: str,
    port: int,
    preset_tools: list[str] | None = None,
) -> None:
    # Build the spec once at startup so an unknown preset-tool name or a misaligned
    # schema fails fast here (before serving) instead of on the first request.
    spec = llm_wrapper_spec(
        name=name,
        system_prompt=system_prompt,
        preset_tools=preset_tools or [],
        model=model,
    )
    card = build_agent_card(agent_id, name, description, port)
    executor = LLMAgentExecutor(agent_id, spec, model)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(
        agent_card=card,
        http_handler=request_handler,
    ).build()
    logger.info(
        "llm_handler_starting agent_id=%s port=%d model=%s preset_tools=%s",
        agent_id,
        port,
        model,
        preset_tools or [],
    )
    uvicorn.run(app, host=os.getenv("AGENT_BIND_HOST", "0.0.0.0"), port=port, log_level="warning")
