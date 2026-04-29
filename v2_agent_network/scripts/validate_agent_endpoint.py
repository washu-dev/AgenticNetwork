from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

import httpx
from a2a.types import AgentCard

from v2_agent_network.services.a2a.adapters import a2a_agent_card_to_discovered_agent
from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.server import create_weather_a2a_app
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.common.models import A2AMessage, A2APart, A2ATaskRequest
from v2_agent_network.services.directory.card_oasf_converter import (
    agent_card_to_oasf_record,
    load_agent_card,
    validate_agent_card,
)
from v2_agent_network.services.directory.client import DirectoryClient, DirectoryUnavailable


async def validate_endpoint(
    *,
    card_path: Path,
    skill: str,
    prompt: str,
    mock_agent: str | None = None,
    endpoint: str | None = None,
    context_id: str | None = None,
    sender_id: str = "validator",
    timeout: float = 30.0,
    publish: bool = False,
    fake_ads: bool | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "card_valid": False,
        "oasf_conversion": False,
        "endpoint_locator": False,
        "validation_mode": mock_agent or "external",
        "endpoint_reachable": False,
        "a2a_task_sent": False,
        "a2a_task_state": None,
        "task_id": None,
        "context_id": context_id,
        "artifacts_count": 0,
        "structured_output_present": False,
        "published": False,
        "publish_backend": None,
        "warnings": [],
        "errors": [],
    }
    try:
        card = load_agent_card(card_path)
        if endpoint:
            card = _card_with_endpoint(card, endpoint)
        validate_agent_card(card)
        _validate_skill(card, skill)
        report["card_valid"] = True
        report["endpoint_locator"] = bool(card.url)

        record = agent_card_to_oasf_record(card)
        report["oasf_conversion"] = True
        report["record_id"] = record["record_id"]

        if publish:
            published = DirectoryClient(fake_ads=fake_ads).publish_oasf_record(record)
            report["published"] = True
            report["publish_backend"] = published.backend
            report["publish_ref"] = published.backend_ref or published.record_id

        context = context_id or f"ctx-validate-{uuid4()}"
        report["context_id"] = context
        if mock_agent:
            result = await _send_mock_task(
                card=card,
                skill=skill,
                prompt=prompt,
                context_id=context,
                sender_id=sender_id,
                mock_agent=mock_agent,
            )
            report["endpoint_reachable"] = True
        else:
            result = await _send_external_task(
                card=card,
                skill=skill,
                prompt=prompt,
                context_id=context,
                sender_id=sender_id,
                timeout=timeout,
            )
            report["endpoint_reachable"] = True

        report["a2a_task_sent"] = True
        report["a2a_task_state"] = result.state.value if hasattr(result.state, "value") else result.state
        report["task_id"] = result.task_id
        report["artifacts_count"] = len(result.artifacts)
        report["structured_output_present"] = bool(result.structured_output)
        if not result.task_id:
            report["errors"].append("A2A response did not include task_id")
        if not result.context_id:
            report["errors"].append("A2A response did not include context_id")
        if not result.artifacts and not result.structured_output:
            report["errors"].append("A2A response did not include artifacts or structured output")
    except DirectoryUnavailable as exc:
        report["errors"].append(str(exc))
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    return report


async def _send_mock_task(
    *,
    card: AgentCard,
    skill: str,
    prompt: str,
    context_id: str,
    sender_id: str,
    mock_agent: str,
) -> Any:
    if mock_agent != "weather":
        raise ValueError(f"Unsupported mock agent: {mock_agent}")
    app = create_weather_a2a_app(card)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=card.url,
    ) as http_client:
        client = A2AClient(HTTPA2ATransport(http_client=http_client))
        return await client.send_task(
            a2a_agent_card_to_discovered_agent(card),
            _request(card=card, skill=skill, prompt=prompt, context_id=context_id, sender_id=sender_id),
        )


async def _send_external_task(
    *,
    card: AgentCard,
    skill: str,
    prompt: str,
    context_id: str,
    sender_id: str,
    timeout: float,
) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        client = A2AClient(HTTPA2ATransport(http_client=http_client))
        return await client.send_task(
            a2a_agent_card_to_discovered_agent(card),
            _request(card=card, skill=skill, prompt=prompt, context_id=context_id, sender_id=sender_id),
        )


def _request(
    *,
    card: AgentCard,
    skill: str,
    prompt: str,
    context_id: str,
    sender_id: str,
) -> A2ATaskRequest:
    agent = a2a_agent_card_to_discovered_agent(card)
    return A2ATaskRequest(
        task_id=str(uuid4()),
        context_id=context_id,
        target_agent_id=agent.agent_id,
        skill=skill,
        user_goal=prompt,
        messages=[A2AMessage(role="user", parts=[A2APart(kind="text", text=prompt)])],
        metadata={"sender_id": sender_id, "skill": skill},
    )


def _card_with_endpoint(card: AgentCard, endpoint: str) -> AgentCard:
    return card.model_copy(update={"url": endpoint})


def _validate_skill(card: AgentCard, skill: str) -> None:
    known = {item.id for item in card.skills}
    known.update(tag for item in card.skills for tag in item.tags)
    if skill not in known:
        raise ValueError(f"Skill {skill} is not declared by card {card.name}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate an A2A Agent Card endpoint.")
    parser.add_argument("--card", required=True, type=Path)
    parser.add_argument("--skill", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--mock-agent", choices=["weather"])
    parser.add_argument("--endpoint")
    parser.add_argument("--context-id")
    parser.add_argument("--sender-id", default="validator")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--fake-ads", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mock_agent and args.endpoint:
        print("--mock-agent and --endpoint are mutually exclusive", file=sys.stderr)
        return 2
    report = asyncio.run(
        validate_endpoint(
            card_path=args.card,
            skill=args.skill,
            prompt=args.prompt,
            mock_agent=args.mock_agent,
            endpoint=args.endpoint,
            context_id=args.context_id,
            sender_id=args.sender_id,
            timeout=args.timeout,
            publish=args.publish,
            fake_ads=True if args.fake_ads else None,
        )
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
