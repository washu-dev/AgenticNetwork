from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from v2_agent_network.services.a2a.server import create_email_a2a_app
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card


DEFAULT_CARD = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "agent_cards"
    / "email_agent.card.json"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the placeholder Email Agent A2A server for ADS/A2A development. "
            "This fixture does not call Gmail, Calendar, Docs, or Sheets."
        )
    )
    parser.add_argument(
        "--host",
        default=os.getenv("EMAIL_AGENT_A2A_HOST", "0.0.0.0"),
        help="Host interface to bind.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("EMAIL_AGENT_A2A_PORT", "9103")),
        help="Port for the placeholder A2A HTTP server.",
    )
    parser.add_argument(
        "--card",
        type=Path,
        default=DEFAULT_CARD,
        help="A2A Agent Card JSON file.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("The uvicorn package is required to run the A2A server.") from exc

    uvicorn.run(
        create_email_a2a_app(load_agent_card(args.card)),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
