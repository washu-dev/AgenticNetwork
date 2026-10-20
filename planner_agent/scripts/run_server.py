from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Sequence

from agentnet.directory.client import DirectoryClient
from planner_agent.main_agent.config import load_config
from planner_agent.main_agent.env import log_old_names, setting
from planner_agent.main_agent.server import create_app


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the planner agent server.")
    parser.add_argument("--host", default=setting("CHAT_APP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(setting("CHAT_APP_PORT", "8090")))
    parser.add_argument(
        "--card",
        action="append",
        type=Path,
        default=[],
        help="Agent Card JSON path to pre-load (fake-ADS mode only). Repeatable.",
    )
    parser.add_argument(
        "--fake-ads",
        action="store_true",
        help="Use process-local fake ADS instead of the real ADS daemon.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.fake_ads:
        os.environ["AGENT_NETWORK_FAKE_ADS"] = "true"
    _configure_logging()
    # Settings read so far (host, port, log level) were read before logging was set up.
    log_old_names()
    cfg = load_config()
    import uvicorn

    directory = DirectoryClient(fake_ads=cfg.fake_ads, ads_url=cfg.ads_server_addr)
    app = create_app(
        config=cfg,
        directory_client=directory,
        agent_card_paths=args.card or None,
    )
    logging.getLogger(__name__).info(
        "main_agent_server_start host=%s port=%s fake_ads=%s ads=%s model=%s",
        args.host,
        args.port,
        cfg.fake_ads,
        cfg.ads_server_addr,
        cfg.llm_model,
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _configure_logging() -> None:
    level = getattr(logging, setting("CHAT_APP_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
