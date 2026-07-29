"""Tool-loop handler — A2A server for an uploaded LLM tool loop.

The registration ``tool_loop`` agent type runs author-supplied tool bodies +
schemas + a system prompt through the shared engine
(``agent_skeleton.llm_loop.run_agent`` via ``spec.tool_loop_spec``). It reuses the
``llm`` handler's ``LLMAgentExecutor`` + card builder verbatim — the run loop and
the A2A dual-channel output are identical; only the spec (the uploaded tools)
differs. This mirrors how ``operation_handler`` (endpoint_ops) is built: the same
executor, a different spec.

This is the LOCAL (subprocess) path. The cluster equivalent is
``agent_skeleton.container_runner._serve_tool_loop`` (which can't import
registration_service, so it serves via the skeleton's own ``create_app``).
"""

from __future__ import annotations

import logging

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore

from agent_skeleton.spec import load_tool_module, tool_loop_spec

# Reuse the llm handler's executor + card builder verbatim — only the spec differs.
from registration_service.handlers.llm_handler import LLMAgentExecutor, build_agent_card

logger = logging.getLogger(__name__)


def run(
    agent_id: str,
    name: str,
    description: str,
    handler_file_path: str,
    system_prompt: str,
    model: str,
    port: int,
    extra: dict | None = None,
) -> None:
    # Load the uploaded tools module and build the spec ONCE at startup, so a
    # missing tool contract or a schema/function mismatch fails fast here (before
    # serving) rather than on the first request.
    schemas, registry = load_tool_module(handler_file_path, extra or {})
    spec = tool_loop_spec(
        name=name,
        system_prompt=system_prompt,
        tool_schemas=schemas,
        tool_registry=registry,
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
        "tool_loop_handler_starting agent_id=%s port=%d model=%s tools=%d file=%s",
        agent_id,
        port,
        model,
        len(schemas),
        handler_file_path,
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
