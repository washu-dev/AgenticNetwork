"""Standalone entrypoint for a custom agent's per-agent Docker image (cluster path).

Self-contained on purpose: depends only on agent_skeleton + a2a-sdk + uvicorn —
all baked into the generated image — and NOT on registration_service (which is not
installed in the per-agent image). The local-dev SubprocessBackend uses
registration_service.runner + handlers/custom_handler instead; this module is the
container equivalent.

Reads the nested AGENT_CONFIG_JSON envelope (set by ClusterBackend) and loads the
handler from HANDLER_FILE_PATH (default /app/handler.py, which the generated
Dockerfile COPYs in). ADS publishing is done by the registration service's
supervisor, not here — this process only serves A2A.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("agent_skeleton.container_runner")


def _load_handler_class(path: str, class_name: str):
    spec = importlib.util.spec_from_file_location("custom_handler_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load handler module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, class_name):
        raise RuntimeError(f"Class '{class_name}' not found in {path}")
    return getattr(module, class_name)


def main() -> None:
    raw = os.environ.get("AGENT_CONFIG_JSON")
    if not raw:
        logger.error("container_runner_missing_config AGENT_CONFIG_JSON not set")
        sys.exit(1)

    cfg = json.loads(raw)
    config: dict = cfg.get("config", {})
    agent_id: str = cfg.get("agent_id", "custom")
    port = int(cfg.get("port") or os.environ.get("AGENT_PORT", "0"))
    class_name: str = config["class_name"]
    extra: dict = config.get("extra", {})
    name: str = config.get("name", agent_id)
    description: str = config.get("description", "")
    handler_file = os.environ.get("HANDLER_FILE_PATH", "/app/handler.py")

    logger.info(
        "container_runner_start agent_id=%s port=%d class=%s file=%s",
        agent_id, port, class_name, handler_file,
    )

    handler_class = _load_handler_class(handler_file, class_name)
    handler = handler_class(extra)

    from a2a.server.apps import A2AStarletteApplication
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks import InMemoryTaskStore
    from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentSkill
    import uvicorn

    from agent_skeleton import HandlerExecutor

    executor = HandlerExecutor(
        handler,
        max_runtime_seconds=float(config.get("max_runtime_seconds", 1800)),
    )
    # P4a: mirror the declared-credentials carrier on the served card. Inlined
    # (not shared with registration_service.card_credentials) because this runs
    # inside the custom agent's own image where that package isn't importable.
    # URI must match registration_service.card_credentials.CREDENTIALS_EXTENSION_URI.
    credentials: list = config.get("credentials", [])
    extensions = (
        [AgentExtension(
            uri="agenticnetwork.dev/ext/required-credentials",
            description="User credentials this agent consumes",
            required=False,
            params={"credentials": credentials},
        )]
        if credentials else None
    )
    card = AgentCard(
        name=name,
        description=description,
        url=f"http://localhost:{port}/",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, extensions=extensions),  # heartbeat support (P3)
        skills=[AgentSkill(id=f"{agent_id}/handle", name="handle", description=description, tags=[])],
        default_input_modes=["text"],
        default_output_modes=["text"],
    )
    app = A2AStarletteApplication(
        agent_card=card,
        http_handler=DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore()),
    ).build()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
