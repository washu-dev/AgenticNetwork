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


def _tool_loop_card(agent_id: str, name: str, description: str, port: int):
    from a2a.types import AgentCapabilities, AgentCard, AgentSkill

    # streaming=True matches the card the supervisor publishes; the planner then
    # sends message/stream (SkeletonAgentExecutor sends heartbeats).
    return AgentCard(
        name=name,
        description=description,
        url=f"http://localhost:{port}/",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        skills=[AgentSkill(id=f"{agent_id}/respond", name="respond", description=description, tags=[])],
        default_input_modes=["text"],
        default_output_modes=["text"],
    )


def _serve_tool_loop(cfg: dict) -> None:
    """Serve an uploaded LLM tool loop (registration ``tool_loop`` type) on the cluster.

    Loads the author's tools module (TOOL_SCHEMAS + TOOL_REGISTRY, or build_tools),
    builds an AgentSpec, and serves it through the skeleton's own create_app +
    SkeletonAgentExecutor — the same engine the local tool_loop_handler uses, but
    without any registration_service import (which isn't in this image)."""
    import uvicorn

    from agent_skeleton.serve import create_app
    from agent_skeleton.spec import load_tool_module, tool_loop_spec

    config: dict = cfg.get("config", {})
    agent_id: str = cfg.get("agent_id", "tool_loop")
    port = int(cfg.get("port") or os.environ.get("AGENT_PORT", "0"))
    name: str = config.get("name", agent_id)
    description: str = config.get("description", "")
    system_prompt: str = config.get("system_prompt", "")
    model = config.get("model")

    # `entry_file` and `extra` live under config.tools in the canonical `llm` shape
    # and directly on config in the legacy `tool_loop` shape. Read whichever is
    # present so one image works with either service version.
    tools_cfg = config.get("tools")
    tools_cfg = tools_cfg if isinstance(tools_cfg, dict) else {}
    entry_file = tools_cfg.get("entry_file") or config.get("entry_file") or "tools.py"
    extra = tools_cfg.get("extra") or config.get("extra") or {}

    handler_file = os.environ.get("HANDLER_FILE_PATH", "/app/" + entry_file)

    logger.info(
        "container_runner_tool_loop_start agent_id=%s port=%d file=%s",
        agent_id, port, handler_file,
    )

    schemas, registry = load_tool_module(handler_file, extra)
    spec = tool_loop_spec(
        name=name,
        system_prompt=system_prompt,
        tool_schemas=schemas,
        tool_registry=registry,
        model=model,
    )
    app = create_app(_tool_loop_card(agent_id, name, description, port), model=model, spec=spec)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


def main() -> None:
    raw = os.environ.get("AGENT_CONFIG_JSON")
    if not raw:
        logger.error("container_runner_missing_config AGENT_CONFIG_JSON not set")
        sys.exit(1)

    cfg = json.loads(raw)

    # An uploaded TOOL MODULE runs an LLM tool loop, not an AgentHandler subclass.
    # Serve it via the skeleton's own spec engine (create_app), which is baked into
    # this image — registration_service is NOT installed here, so we sniff the shape
    # of the envelope rather than importing its models.
    #
    # Two envelope shapes reach us, and both must work: the CANONICAL one
    # (handler_type='llm' with config.tools.source='upload') and the legacy
    # pre-consolidation one (handler_type='tool_loop'). A deployed image is
    # long-lived and may be launched by an older or newer service, so accept both.
    config_probe: dict = cfg.get("config") or {}
    tools_probe = config_probe.get("tools")
    is_uploaded_tool_loop = (
        cfg.get("handler_type") == "tool_loop"
        or (isinstance(tools_probe, dict) and tools_probe.get("source") == "upload")
    )
    if is_uploaded_tool_loop:
        _serve_tool_loop(cfg)
        return

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
