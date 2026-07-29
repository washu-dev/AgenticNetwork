"""Agent runner — entry point for spawned agent subprocesses.

Reads AGENT_CONFIG_JSON from the environment, dispatches to the right handler.
This script is exec'd by SubprocessBackend.spawn() for each registered agent.
"""

from __future__ import annotations

import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    raw = os.getenv("AGENT_CONFIG_JSON")
    if not raw:
        logger.error("runner_missing_config AGENT_CONFIG_JSON not set")
        sys.exit(1)

    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("runner_invalid_json error=%s", exc)
        sys.exit(1)

    agent_id: str = cfg["agent_id"]
    handler_type: str = cfg["handler_type"]
    port: int = int(cfg["port"])
    config: dict = cfg["config"]

    logger.info("runner_start agent_id=%s handler_type=%s port=%d", agent_id, handler_type, port)

    if handler_type == "llm":
        from registration_service.handlers.llm_handler import run

        run(
            agent_id=agent_id,
            name=config.get("name", agent_id),
            description=config.get("description", ""),
            system_prompt=config["system_prompt"],
            model=config.get("model", "openai/gpt-oss-120b"),
            port=port,
            preset_tools=config.get("preset_tools", []),
        )
    elif handler_type == "api_proxy":
        from registration_service.handlers.api_proxy_handler import run

        run(
            agent_id=agent_id,
            name=config.get("name", agent_id),
            description=config.get("description", ""),
            upstream_url=config["upstream_url"],
            auth_header=config.get("auth_header"),
            port=port,
        )
    elif handler_type == "endpoint_ops":
        from registration_service.handlers.operation_handler import run

        run(
            agent_id=agent_id,
            name=config.get("name", agent_id),
            description=config.get("description", ""),
            base_url=config["base_url"],
            operations=config.get("operations", []),
            auth_env=config.get("auth_env"),
            auth_scheme=config.get("auth_scheme", "Bearer"),
            system_prompt=config.get("system_prompt"),
            model=config.get("model", "openai/gpt-oss-120b"),
            port=port,
        )
    elif handler_type == "custom":
        from registration_service.handlers.custom_handler import run

        handler_file_path = os.environ.get("HANDLER_FILE_PATH")
        if not handler_file_path:
            logger.error("runner_missing_handler_file HANDLER_FILE_PATH not set")
            sys.exit(1)

        run(
            agent_id=agent_id,
            name=config.get("name", agent_id),
            description=config.get("description", ""),
            handler_file_path=handler_file_path,
            class_name=config["class_name"],
            extra=config.get("extra", {}),
            port=port,
            max_runtime_seconds=float(config.get("max_runtime_seconds", 1800)),
            credentials=config.get("credentials", []),
        )
    elif handler_type == "tool_loop":
        from registration_service.handlers.tool_loop_handler import run

        handler_file_path = os.environ.get("HANDLER_FILE_PATH")
        if not handler_file_path:
            logger.error("runner_missing_handler_file HANDLER_FILE_PATH not set")
            sys.exit(1)

        run(
            agent_id=agent_id,
            name=config.get("name", agent_id),
            description=config.get("description", ""),
            handler_file_path=handler_file_path,
            system_prompt=config["system_prompt"],
            model=config.get("model", "openai/gpt-oss-120b"),
            port=port,
            extra=config.get("extra", {}),
        )
    else:
        logger.error("runner_unknown_handler_type handler_type=%s", handler_type)
        sys.exit(1)


if __name__ == "__main__":
    main()
