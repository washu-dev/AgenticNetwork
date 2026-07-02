"""Custom handler — A2A server backed by a user-uploaded AgentHandler subclass.

Loads the handler class from the uploaded file (HANDLER_FILE_PATH), instantiates
it with the `extra` config, and wraps it in agent_skeleton.HandlerExecutor. The
A2A serving wiring mirrors llm_handler / api_proxy_handler.
"""

from __future__ import annotations

import importlib.util
import logging

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from agent_skeleton import HandlerExecutor
from registration_service.card_credentials import build_credentials_extension

logger = logging.getLogger(__name__)


def _load_handler_class(handler_file_path: str, class_name: str):
    spec = importlib.util.spec_from_file_location("custom_handler_module", handler_file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load handler module from {handler_file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, class_name):
        raise RuntimeError(f"Class '{class_name}' not found in {handler_file_path}")
    return getattr(module, class_name)


def build_agent_card(
    agent_id: str,
    name: str,
    description: str,
    port: int,
    credentials: list[dict] | None = None,
) -> AgentCard:
    # P4a: mirror the ADS-card credential carrier on the served card for direct
    # callers (the planner reads the ADS-published card, not this one).
    ext = build_credentials_extension(credentials)
    return AgentCard(
        name=name,
        description=description,
        url=f"http://localhost:{port}/",
        version="1.0.0",
        capabilities=AgentCapabilities(  # heartbeat support (P3)
            streaming=True,
            extensions=[ext] if ext else None,
        ),
        skills=[
            AgentSkill(
                id=f"{agent_id}/handle",
                name="handle",
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
    handler_file_path: str,
    class_name: str,
    extra: dict,
    port: int,
    max_runtime_seconds: float = 1800.0,
    credentials: list[dict] | None = None,
) -> None:
    handler_class = _load_handler_class(handler_file_path, class_name)
    handler = handler_class(extra)
    executor = HandlerExecutor(handler, max_runtime_seconds=max_runtime_seconds)

    card = build_agent_card(agent_id, name, description, port, credentials)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(
        agent_card=card,
        http_handler=request_handler,
    ).build()
    logger.info(
        "custom_handler_starting agent_id=%s port=%d class=%s file=%s",
        agent_id, port, class_name, handler_file_path,
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
