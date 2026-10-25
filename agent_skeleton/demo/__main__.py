"""Run the demo agent.  DEMO AGENT

    python -m agent_skeleton.demo check    # validate the demo's schemas <-> functions
    python -m agent_skeleton.demo serve    # serve the demo agent over A2A

Run from the AgenticNetwork repo root (or with the editable install): a regular
install leaves the demo out.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..serve import _add_serve_args, _serve
from . import CARD_PATH, demo_spec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="The agent runtime's demo agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Validate the demo's tool schema/function alignment and exit.")
    serve = sub.add_parser("serve", help="Run the demo agent's A2A HTTP server.")
    _add_serve_args(serve)
    serve.add_argument("--card", type=Path, default=CARD_PATH)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    spec = demo_spec()
    if args.command == "check":
        spec.validate()
        print("OK: tool schemas and functions are aligned.")
        return
    _serve(args, spec=spec)


if __name__ == "__main__":
    main()
