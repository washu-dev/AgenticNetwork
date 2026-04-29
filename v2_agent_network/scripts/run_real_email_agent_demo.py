from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Sequence

import httpx

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app
from v2_agent_network.services.state.store import InMemoryStateStore


DEFAULT_CARD = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "agent_cards"
    / "email_agent.card.json"
)
CURRENT_TIME_PROMPT = "What time is it for the email agent?"
DRAFT_PROMPT = (
    "Draft a short email to myself saying this is an AgenticNetwork A2A "
    "integration test. Do not send it."
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a safe PI -> live Email Agent A2A integration demo."
    )
    parser.add_argument("--card", type=Path, default=DEFAULT_CARD)
    parser.add_argument("--endpoint", help="Override the Agent Card endpoint URL.")
    parser.add_argument(
        "--skip-draft",
        action="store_true",
        help="Only run the read-only email/current_time demo.",
    )
    return parser.parse_args(argv)


async def run_demo(args: argparse.Namespace) -> dict[str, Any]:
    os.environ.setdefault("AGENT_NETWORK_FAKE_ADS", "true")
    card = load_agent_card(args.card)
    if args.endpoint:
        card = card.model_copy(update={"url": args.endpoint})

    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(card)
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
        current_time = await _send_pi_message(
            pi_http_client=pi_http_client,
            state_store=state_store,
            card=card,
            skill="email/current_time",
            text=CURRENT_TIME_PROMPT,
        )
        results: dict[str, Any] = {"email_current_time": current_time}
        if not args.skip_draft:
            results["email_draft"] = await _send_pi_message(
                pi_http_client=pi_http_client,
                state_store=state_store,
                card=card,
                skill="email/draft",
                text=DRAFT_PROMPT,
                metadata={"create_gmail_draft": False},
            )
    return results


async def _send_pi_message(
    *,
    pi_http_client: httpx.AsyncClient,
    state_store: InMemoryStateStore,
    card,
    skill: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await pi_http_client.post(
        "/messages",
        json={
            "user_id": "real-email-demo",
            "channel": "api",
            "text": text,
            "requested_skill": skill,
            "metadata": metadata or {},
        },
    )
    response.raise_for_status()
    payload = response.json()
    task_id = payload["task_ids"].get(skill)
    if not task_id:
        raise RuntimeError(f"PI response did not include a task id for {skill}: {payload}")
    invocation = state_store.get_invocation(task_id)
    expected_endpoint = card.url.rstrip("/")
    if invocation.selected_agent.endpoint.rstrip("/") != expected_endpoint:
        raise RuntimeError(
            f"Selected endpoint {invocation.selected_agent.endpoint} did not match {card.url}."
        )
    expected_org = card.provider.organization if card.provider else ""
    if invocation.selected_agent.organization != expected_org:
        raise RuntimeError(
            "Selected agent organization "
            f"{invocation.selected_agent.organization!r} did not match {expected_org!r}."
        )
    _assert_not_placeholder(payload)
    return payload


def _assert_not_placeholder(payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, sort_keys=True)
    forbidden = [
        "Placeholder executor only",
        "placeholder",
        "no Gmail draft was created",
    ]
    for marker in forbidden:
        if marker in serialized:
            raise RuntimeError(f"Response appears to come from the placeholder Email Agent: {marker}")


def main(argv: Sequence[str] | None = None) -> int:
    results = asyncio.run(run_demo(parse_args(argv)))
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
