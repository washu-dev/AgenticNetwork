import logging
import os
import signal

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


# Custom logging config for uvicorn that completely silences uvicorn.access.
# uvicorn applies this via logging.config.dictConfig() at server start, so
# whatever handler structlog / fastapi install on uvicorn.access gets
# overwritten here.
_UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn":       {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        # Nuke access logger: no handlers, highest level, no propagation.
        "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
    },
}


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
        log_config=_UVICORN_LOG_CONFIG,
        # Force-close any lingering connections (idle WebSockets, etc.)
        # 3s after Ctrl+C, so one ^C is enough to actually exit.
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()
