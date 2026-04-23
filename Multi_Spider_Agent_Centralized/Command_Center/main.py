import asyncio
import logging
import os
import signal
import time

import uvicorn

from Command_Center.config import GATEWAY_PORT

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
        access_log=False,
    )


if __name__ == "__main__":
    main()
