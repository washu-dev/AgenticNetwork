from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Sequence

from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the PI Agent HTTP server.")
    parser.add_argument("--host", default=os.getenv("PI_AGENT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PI_AGENT_PORT", "8080")))
    parser.add_argument(
        "--card",
        action="append",
        type=Path,
        default=[],
        help="Agent Card JSON path to load into the PI process DirectoryClient. Repeatable.",
    )
    parser.add_argument(
        "--fake-ads",
        action="store_true",
        help="Use process-local fake ADS for this PI server.",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Enable the PI LLM planner for this server. OPENAI_API_KEY must be set.",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="Override PI_LLM_MODEL for this server.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.fake_ads:
        os.environ["AGENT_NETWORK_FAKE_ADS"] = "true"
    if args.llm:
        os.environ["PI_LLM_ENABLED"] = "true"
    if args.llm_model:
        os.environ["PI_LLM_MODEL"] = args.llm_model
    _configure_logging()
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("The uvicorn package is required to run the PI Agent server.") from exc

    app = create_app(
        directory_client=DirectoryClient(fake_ads=True if args.fake_ads else None),
        publish_weather_card=False,
        publish_calendar_email_cards=False,
        publish_geo_intelligence_cards=False,
        publish_coding_card=False,
        agent_card_paths=args.card or None,
    )
    logging.getLogger(__name__).info(
        "pi_server_start host=%s port=%s fake_ads=%s llm_enabled=%s ads_server=%s",
        args.host,
        args.port,
        os.getenv("AGENT_NETWORK_FAKE_ADS", "false"),
        os.getenv("PI_LLM_ENABLED", "false"),
        os.getenv("ADS_SERVER_ADDR") or os.getenv("ADS_URL"),
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _configure_logging() -> None:
    level_name = os.getenv("PI_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
