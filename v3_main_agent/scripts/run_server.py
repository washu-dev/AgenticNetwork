from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Sequence

from v3_main_agent.main_agent.directory.client import DirectoryClient
from v3_main_agent.main_agent.config import load_config
from v3_main_agent.main_agent.server import create_app


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the v3 Main Agent server.")
    parser.add_argument("--host", default=os.getenv("MAIN_AGENT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MAIN_AGENT_PORT", "8090")))
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
    cfg = load_config()
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required to run the v3 main agent server.") from exc
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
    level = getattr(logging, os.getenv("MAIN_AGENT_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
