from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Sequence

import httpx

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app
from v2_agent_network.services.state.store import InMemoryStateStore

DEFAULT_PROMPT = "What time is it for the email agent?"
WRITE_SKILLS = {
    "email/send",
    "email/calendar/create_event",
    "email/docs/create",
    "email/docs/append",
    "email/docs/replace_text",
    "email/docs/create_and_email",
    "email/sheets/create",
    "email/sheets/write",
    "email/sheets/append",
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a safe LLM PI -> real ADS -> live Email Agent demo."
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--expected-skill",
        default="email/current_time",
        help="Safe skill expected in the LLM plan.",
    )
    return parser.parse_args(argv)


async def run_demo(args: argparse.Namespace) -> dict[str, Any]:
    _require_real_ads_env()
    _require_llm_env()
    if args.expected_skill in WRITE_SKILLS:
        raise SystemExit(f"{args.expected_skill} is a write skill and is blocked by this demo.")
    os.environ.pop("PI_AGENT_CARD_PATHS", None)

    state_store = InMemoryStateStore()
    pi_app = create_app(
        directory_client=DirectoryClient(fake_ads=False),
        a2a_client=A2AClient(HTTPA2ATransport()),
        state_store=state_store,
        publish_weather_card=False,
        publish_calendar_email_cards=False,
        publish_geo_intelligence_cards=False,
        publish_coding_card=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
        timeout=60,
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "llm-pi-email-demo",
                "channel": "api",
                "text": args.prompt,
                "metadata": {"create_gmail_draft": False},
            },
        )
    response.raise_for_status()
    payload = response.json()
    workflow = payload.get("workflow", [])
    if args.expected_skill not in workflow:
        raise RuntimeError(f"Expected {args.expected_skill!r} in workflow, got {workflow!r}.")
    if payload.get("confirmation_required"):
        raise RuntimeError(f"Safe demo unexpectedly required confirmation: {payload}")
    return payload


def _require_real_ads_env() -> None:
    if os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() != "false":
        raise SystemExit("Set AGENT_NETWORK_FAKE_ADS=false for the LLM real ADS demo.")
    if not (os.getenv("ADS_SERVER_ADDR") or os.getenv("ADS_URL")):
        raise SystemExit("Set ADS_SERVER_ADDR=localhost:8888 or ADS_URL=localhost:8888.")


def _require_llm_env() -> None:
    if os.getenv("PI_LLM_ENABLED", "false").lower() != "true":
        raise SystemExit("Set PI_LLM_ENABLED=true for this demo.")
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY in the PI server environment.")


def main(argv: Sequence[str] | None = None) -> int:
    results = asyncio.run(run_demo(parse_args(argv)))
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
