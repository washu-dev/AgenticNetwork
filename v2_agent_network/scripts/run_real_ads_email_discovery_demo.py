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

DEFAULT_ENDPOINT = "http://10.229.117.223:9103/"
DEFAULT_PROMPT = "What time is it for the email agent?"
SAFE_SKILLS = {"email/current_time", "email/google/status", "email/draft"}
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
        description="Run a safe PI -> real ADS -> live Email Agent discovery demo."
    )
    parser.add_argument("--skill", default="email/current_time")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help="Expected live Email Agent endpoint. The ADS record must already use this URL.",
    )
    return parser.parse_args(argv)


async def run_demo(args: argparse.Namespace) -> dict[str, Any]:
    _require_safe_skill(args.skill)
    _require_real_ads_env()
    os.environ.pop("PI_AGENT_CARD_PATHS", None)

    directory = DirectoryClient(fake_ads=False)
    discovered_agents = directory.search_agents(skill=args.skill)
    if not discovered_agents:
        raise RuntimeError(f"No Email Agent discovered from real ADS for skill {args.skill}.")
    discovered = discovered_agents[0]
    resolved = directory.resolve_agent(discovered.source_record_id)
    expected_endpoint = args.endpoint.rstrip("/")
    if resolved.endpoint.rstrip("/") != expected_endpoint:
        raise RuntimeError(
            f"Resolved ADS endpoint {resolved.endpoint!r} did not match {args.endpoint!r}."
        )

    state_store = InMemoryStateStore()
    pi_app = create_app(
        directory_client=directory,
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
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "real-ads-email-demo",
                "channel": "api",
                "text": args.prompt,
                "requested_skill": args.skill,
                "metadata": _safe_metadata_for_skill(args.skill),
            },
        )
    response.raise_for_status()
    payload = response.json()
    task_id = payload.get("task_ids", {}).get(args.skill)
    if not task_id:
        raise RuntimeError(f"PI response did not include a task id for {args.skill}: {payload}")
    invocation = state_store.get_invocation(task_id)
    if invocation.selected_agent.endpoint.rstrip("/") != expected_endpoint:
        raise RuntimeError(
            "PI selected endpoint "
            f"{invocation.selected_agent.endpoint!r}, expected {args.endpoint!r}."
        )
    if payload.get("confirmation_required"):
        raise RuntimeError(f"Safe read-only demo unexpectedly required confirmation: {payload}")
    return {
        "ads_discovered_agent": discovered.model_dump(mode="json"),
        "ads_resolved_agent": resolved.model_dump(mode="json"),
        "pi_response": payload,
        "selected_endpoint": invocation.selected_agent.endpoint,
        "selected_agent": invocation.selected_agent.agent_id,
        "task_id": task_id,
        "task_state": payload.get("state"),
        "artifacts": payload.get("artifacts", []),
        "structured_output": payload.get("structured_output"),
        "side_effects": "none requested; write skills are refused by this demo",
    }


def _require_safe_skill(skill: str) -> None:
    if skill in WRITE_SKILLS:
        raise SystemExit(f"{skill} is a side-effect skill and is blocked by this demo.")
    if skill not in SAFE_SKILLS:
        raise SystemExit(
            f"{skill} is not on the safe automated demo list. Use one of: {', '.join(sorted(SAFE_SKILLS))}."
        )


def _require_real_ads_env() -> None:
    if os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() != "false":
        raise SystemExit("Set AGENT_NETWORK_FAKE_ADS=false for the real ADS discovery demo.")
    if not (os.getenv("ADS_SERVER_ADDR") or os.getenv("ADS_URL")):
        raise SystemExit("Set ADS_SERVER_ADDR=localhost:8888 or ADS_URL=localhost:8888.")


def _safe_metadata_for_skill(skill: str) -> dict[str, Any]:
    if skill == "email/draft":
        return {"create_gmail_draft": False}
    return {}


def main(argv: Sequence[str] | None = None) -> int:
    results = asyncio.run(run_demo(parse_args(argv)))
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
