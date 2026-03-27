import asyncio
import logging
import os
import signal
import time

import uvicorn

from Command_Center.config import (
    GATEWAY_PORT, HEARTBEAT_TIMEOUT, CLEANUP_INTERVAL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Suppress noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

_ctrl_c_count = 0


def _handle_sigint(_signum, _frame):
    global _ctrl_c_count
    _ctrl_c_count += 1
    if _ctrl_c_count >= 2:
        print("\nForce exit.")
        os._exit(1)
    print("\nShutting down... (press Ctrl+C again to force)")
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGINT, _handle_sigint)

    print(f"""
╔══════════════════════════════════════════════════╗
║   Multi-Spider Agent Command Center              ║
║   All intelligence centralized                   ║
║   Port: {GATEWAY_PORT:<41}║
╚══════════════════════════════════════════════════╝
""")

    uvicorn.run(
        "Command_Center.gateway:app",
        host="0.0.0.0",
        port=GATEWAY_PORT,
        reload=False,
        log_config=_uvicorn_log_config(),
    )


def _uvicorn_log_config():
    import copy
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config["filters"] = {
        "skip_polls": {
            "()": "Command_Center.main.PollFilter",
        },
    }
    config["handlers"]["access"]["filters"] = ["skip_polls"]
    return config


class PollFilter(logging.Filter):
    """Filter out noisy polling endpoints from access logs."""
    _SKIP = ("/api/robots", "/api/agents", "/sw.js", "/api/mission/active")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._SKIP)


if __name__ == "__main__":
    main()
