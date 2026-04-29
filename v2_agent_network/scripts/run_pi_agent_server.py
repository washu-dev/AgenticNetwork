from __future__ import annotations

import argparse
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.fake_ads:
        os.environ["AGENT_NETWORK_FAKE_ADS"] = "true"
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
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
