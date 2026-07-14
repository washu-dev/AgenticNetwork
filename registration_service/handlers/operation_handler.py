"""Endpoint-ops handler — A2A server for a TYPED per-operation API wrapper.

The registration ``endpoint_ops`` agent type wraps an external HTTP/JSON API by
exposing each of its operations as its OWN typed Chat Completions tool (Feature B):
the model calls e.g. ``get_weather(city=...)`` and the tool builds the concrete
``GET /weather?city=...`` request. This is built on the same shared engine as the
``llm`` type — it reuses ``agent_skeleton.llm_loop.run_agent`` via
``operation_wrapper_spec`` — so the serving stack (executor, dual-channel output)
is identical; only the spec (typed operation tools) differs.
"""

from __future__ import annotations

import logging

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore

from agent_skeleton.spec import operation_wrapper_spec
from agent_skeleton.system_tools.call_endpoint import EndpointConfig, OperationConfig

# Reuse the llm handler's executor + card builder verbatim — the run loop and A2A
# dual-channel output are identical; only the spec (typed operation tools) differs.
from registration_service.handlers.llm_handler import LLMAgentExecutor, build_agent_card

logger = logging.getLogger(__name__)


def run(
    agent_id: str,
    name: str,
    description: str,
    base_url: str,
    operations: list[dict],
    port: int,
    auth_env: str | None = None,
    auth_scheme: str = "Bearer",
    system_prompt: str | None = None,
    model: str = "openai/gpt-oss-120b",
) -> None:
    # The base carries the shared connection concerns (URL, auth, timeout); each
    # OperationConfig carries the method/path/typed params. Build the spec ONCE at
    # startup so a bad operation (misaligned schema, empty list) fails fast here
    # rather than on the first request.
    base = EndpointConfig(url=base_url, auth_env=auth_env, auth_scheme=auth_scheme)
    ops = [OperationConfig(**op) for op in operations]
    spec = operation_wrapper_spec(
        name=name,
        base=base,
        operations=ops,
        description=description,
        system_prompt=system_prompt,
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
        "operation_handler_starting agent_id=%s port=%d model=%s operations=%d base_url=%s",
        agent_id,
        port,
        model,
        len(ops),
        base_url,
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
