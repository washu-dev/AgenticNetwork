"""LLM handler — minimal A2A server backed by a single-turn Chat Completions call."""

from __future__ import annotations

import json
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

logger = logging.getLogger(__name__)


class LLMAgentExecutor(AgentExecutor):
    """Single-turn LLM executor: system prompt from config, user text from A2A task."""

    def __init__(self, agent_id: str, system_prompt: str, model: str) -> None:
        self._agent_id = agent_id
        self._system_prompt = system_prompt
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
            reply = await _call_llm(self._system_prompt, self._model, user_text)
        except Exception as exc:
            logger.exception("llm_handler_failed agent_id=%s", self._agent_id)
            await updater.complete(
                message=updater.new_agent_message(
                    [Part(root=TextPart(text=f"LLM call failed: {exc}"))]
                )
            )
            return

        output = {"agent_id": self._agent_id, "response": reply}
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


async def _call_llm(system_prompt: str, model: str, user_text: str) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI()  # reads OPENAI_API_KEY / OPENAI_BASE_URL from env
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        max_tokens=2048,
    )
    message = response.choices[0].message
    return str(message.content or "").strip()


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
        capabilities=AgentCapabilities(streaming=False),
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


def run(agent_id: str, name: str, description: str, system_prompt: str, model: str, port: int) -> None:
    card = build_agent_card(agent_id, name, description, port)
    executor = LLMAgentExecutor(agent_id, system_prompt, model)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(
        agent_card=card,
        http_handler=request_handler,
    ).build()
    logger.info("llm_handler_starting agent_id=%s port=%d model=%s", agent_id, port, model)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
