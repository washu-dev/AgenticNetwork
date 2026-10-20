"""Agent runner — entry point for spawned agent subprocesses.

Reads AGENT_CONFIG_JSON from the environment, dispatches to the right handler.
This script is exec'd by SubprocessBackend.spawn() for each registered agent.

Dispatch is on the CANONICAL handler type (``llm`` / ``proxy`` / ``custom``) and,
for ``llm``, on ``config.tools.source`` — because "where do the tools come from"
is a field now, not a type:

  * ``proxy``                                → handlers/proxy_handler.py
  * ``llm``, tools.source none/presets/raw   → handlers/llm_handler.py
  * ``llm``, tools.source upload             → handlers/llm_upload_handler.py
  * ``llm``, tools.source http               → handlers/llm_http_handler.py
  * ``custom``                               → handlers/custom_handler.py

The legacy names (``api_proxy``, ``tool_loop``, ``endpoint_ops``) and their old
config shapes are still read here (``models.parse_config`` +
``normalize_handler_type``). SubprocessBackend sends the canonical name with the
canonical config (``backend._config_envelope``), so an agent row written before
the consolidation still starts unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def _handler_file_path() -> str:
    path = os.environ.get("HANDLER_FILE_PATH")
    if not path:
        logger.error("runner_missing_handler_file HANDLER_FILE_PATH not set")
        sys.exit(1)
    return path


def _exit_with_parent() -> None:
    """Exit when the registration service that spawned this agent is gone.

    SubprocessBackend starts each agent in its own session, so the service's
    shutdown can stop agents in order and a terminal Ctrl-C reaches only the
    service. The cost: if the service is SIGKILLed or crashes, no signal reaches
    the agents. The backend passes its pid in AGENT_PARENT_PID; once this process
    has been re-parented, the service has died, so exit rather than linger as an
    orphan holding the port.
    """
    raw = os.getenv("AGENT_PARENT_PID")
    if not raw:
        return
    parent = int(raw)

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(1.0)
        logger.warning("runner_parent_exited parent_pid=%d", parent)
        os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def main() -> None:
    _exit_with_parent()

    from pydantic import ValidationError

    from registration_service.models import (
        LLMConfig,
        ProxyConfig,
        CustomConfig,
        normalize_handler_type,
        parse_config,
    )

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
    wire_type: str = cfg["handler_type"]
    port: int = int(cfg["port"])
    raw_config: dict = cfg["config"]

    handler_type = normalize_handler_type(wire_type)
    # name/description ride along in the envelope but are not config fields.
    name = raw_config.get("name", agent_id)
    description = raw_config.get("description", "")
    try:
        config = parse_config(wire_type, {k: v for k, v in raw_config.items()
                                         if k not in ("name", "description")})
    except ValidationError as exc:
        # Log field locations and messages only: str(exc) echoes input values, and
        # a missing-field error echoes the whole config, which can hold a proxy
        # auth_header or secrets in a custom agent's `extra`.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<config>'}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)
        )
        logger.error("runner_bad_config handler_type=%s error=%s", wire_type, problems)
        sys.exit(1)
    except Exception as exc:
        logger.error("runner_bad_config handler_type=%s error=%s", wire_type, exc)
        sys.exit(1)

    logger.info(
        "runner_start agent_id=%s handler_type=%s (wire=%s) port=%d",
        agent_id, handler_type, wire_type, port,
    )

    if isinstance(config, ProxyConfig):
        from registration_service.handlers.proxy_handler import run

        run(
            agent_id=agent_id,
            name=name,
            description=description,
            upstream_url=config.upstream_url,
            auth_header=config.auth_header,
            port=port,
        )
        return

    if isinstance(config, CustomConfig):
        from registration_service.handlers.custom_handler import run

        run(
            agent_id=agent_id,
            name=name,
            description=description,
            handler_file_path=_handler_file_path(),
            class_name=config.class_name,
            extra=config.extra,
            port=port,
            max_runtime_seconds=float(config.max_runtime_seconds),
            credentials=[c.model_dump() for c in config.credentials],
        )
        return

    if isinstance(config, LLMConfig):
        source = config.tools.source
        if source == "upload":
            # Author-uploaded tool bodies (the old `tool_loop` type).
            from registration_service.handlers.llm_upload_handler import run

            run(
                agent_id=agent_id,
                name=name,
                description=description,
                handler_file_path=_handler_file_path(),
                system_prompt=config.system_prompt or "",
                model=config.model,
                port=port,
                extra=config.tools.extra,
            )
            return
        if source == "http":
            # One typed tool per API operation (the old `endpoint_ops` type).
            from registration_service.handlers.llm_http_handler import run

            run(
                agent_id=agent_id,
                name=name,
                description=description,
                base_url=config.tools.base_url,
                operations=[op.model_dump() for op in config.tools.operations],
                auth_env=config.tools.auth_env,
                auth_scheme=config.tools.auth_scheme,
                system_prompt=config.system_prompt,
                model=config.model,
                port=port,
            )
            return

        # none / presets / raw — a prompt, optionally armed with catalog tools.
        if source == "raw":
            # Schemas with no bodies: the model could emit these calls but nothing
            # can dispatch them, and the engine's strict schema<->function alignment
            # check would reject them. This was silently ignored before the
            # consolidation; keep that behavior (run prompt-only) but say so.
            logger.warning(
                "runner_raw_tool_schemas_not_dispatchable agent_id=%s count=%d "
                "(declared schemas have no function bodies; running prompt-only. "
                "Use tools.source='upload' to ship bodies, or 'presets'/'http'.)",
                agent_id, len(config.tools.schemas),
            )
        from registration_service.handlers.llm_handler import run

        run(
            agent_id=agent_id,
            name=name,
            description=description,
            system_prompt=config.system_prompt or "",
            model=config.model,
            port=port,
            preset_tools=list(getattr(config.tools, "names", []) or []),
        )
        return

    logger.error("runner_unknown_config handler_type=%s config=%s", wire_type, type(config).__name__)
    sys.exit(1)


if __name__ == "__main__":
    main()
