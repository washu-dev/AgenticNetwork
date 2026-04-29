from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.server import create_coding_a2a_app
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.directory.card_oasf_converter import (
    agent_card_to_oasf_record,
    load_agent_card,
    oasf_record_to_discovered_agent,
)
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app
from v2_agent_network.services.pi_agent.policy import evaluate_skill
from v2_agent_network.services.pi_agent.router import plan_workflow
from v2_agent_network.services.state.store import InMemoryStateStore


CODING_CARD = "v2_agent_network/configs/agent_cards/coding_agent.card.json"
READ_ONLY_PROMPT = "Review the current repo and suggest what coding task should be done next."
WRITE_RISK_PROMPT = "Fix the failing test in this repo."


class SpyA2AClient(A2AClient):
    def __init__(self, wrapped: A2AClient):
        self.wrapped = wrapped
        self.calls: list[tuple[str, str]] = []

    async def send_task(self, agent, request):
        self.calls.append((agent.agent_id, request.skill))
        return await self.wrapped.send_task(agent, request)


def test_coding_agent_card_validation() -> None:
    card = load_agent_card(CODING_CARD)
    skill_ids = {skill.id for skill in card.skills}
    assert card.name == "Coding Agent"
    assert "code/repository_inspection" in skill_ids
    assert "code/bug_fix" in skill_ids
    assert "code/pr_review" in skill_ids
    assert "code/test_generation" in skill_ids
    assert "code/documentation" in skill_ids


def test_oasf_conversion_for_coding_card_keeps_custom_skills_and_policy_metadata() -> None:
    record = agent_card_to_oasf_record(load_agent_card(CODING_CARD))

    assert record["discovery"]["official_oasf_skills"] == []
    assert "code/repository_inspection" in record["discovery"]["custom_skill_tags"]
    assert "code/bug_fix" in record["discovery"]["custom_skill_tags"]
    assert record["policy"]["skill_policies"]["code/repository_inspection"]["action_type"] == "read"
    assert record["policy"]["skill_policies"]["code/bug_fix"]["action_type"] == "write"
    assert record["policy"]["skill_policies"]["code/bug_fix"]["confirmation_required"] is True
    discovered = oasf_record_to_discovered_agent(record)
    assert discovered.agent_id == "coding-agent"
    bug_fix = next(cap for cap in discovered.skills if "code/bug_fix" in cap.skill_tags)
    assert bug_fix.action_type == "write"


def test_fake_directory_publish_search_resolve_for_coding_skills() -> None:
    directory = DirectoryClient(fake_ads=True)
    published = directory.publish_agent_card(load_agent_card(CODING_CARD))

    assert directory.search_agents(skill="code/repository_inspection")[0].agent_id == "coding-agent"
    assert directory.search_agents(skill="code/bug_fix")[0].agent_id == "coding-agent"
    assert directory.resolve_agent(published.record_id).agent_id == "coding-agent"


def test_router_maps_coding_prompts() -> None:
    assert plan_workflow(READ_ONLY_PROMPT) == ["code/repository_inspection"]
    assert plan_workflow(WRITE_RISK_PROMPT) == ["code/bug_fix"]


def test_policy_rules_for_coding() -> None:
    assert evaluate_skill("code/repository_inspection").status == "allowed"
    assert evaluate_skill("code/pr_review").status == "allowed"
    assert evaluate_skill("code/documentation").status == "allowed"
    assert evaluate_skill("code/bug_fix").status == "requires_confirmation"
    assert evaluate_skill("code/test_generation").status == "requires_confirmation"


def test_pi_read_only_coding_flow_discovers_and_calls_coding_over_a2a_http() -> None:
    asyncio.run(_pi_read_only_coding_flow_discovers_and_calls_coding_over_a2a_http())


async def _pi_read_only_coding_flow_discovers_and_calls_coding_over_a2a_http() -> None:
    coding_card = load_agent_card(CODING_CARD)
    coding_app = create_coding_a2a_app(coding_card)
    directory = DirectoryClient(fake_ads=True)
    state_store = InMemoryStateStore()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=coding_app),
        base_url=coding_card.url,
    ) as coding_http_client:
        transport = HTTPA2ATransport(
            http_clients_by_origin={_origin(coding_card.url): coding_http_client}
        )
        spy_client = SpyA2AClient(A2AClient(transport))
        pi_app = create_app(
            directory_client=directory,
            a2a_client=spy_client,
            state_store=state_store,
            publish_weather_card=False,
            publish_coding_card=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pi_app),
            base_url="http://pi-agent",
        ) as pi_http_client:
            response = await pi_http_client.post(
                "/messages",
                json={"user_id": "user-1", "channel": "api", "text": READ_ONLY_PROMPT},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert spy_client.calls == [("coding-agent", "code/repository_inspection")]
    assert payload["workflow"] == ["code/repository_inspection"]
    assert payload["selected_agents"] == [
        {
            "skill": "code/repository_inspection",
            "agent_id": "coding-agent",
            "task_id": payload["task_ids"]["code/repository_inspection"],
        }
    ]
    assert payload["confirmation_required"] is False
    assert payload["policy_decision"] == "allowed"
    assert payload["repo_summary"].startswith("Mock inspection:")
    assert payload["detected_issues"]
    assert payload["recommended_next_task"].startswith("Implement Milestone 4")
    assert payload["risk_level"] == "low"
    assert {artifact["artifact_id"] for artifact in payload["artifacts"]} == {
        "coding-repository-inspection"
    }

    invocation = state_store.get_invocation(payload["task_ids"]["code/repository_inspection"])
    context = state_store.get_context(payload["context_id"])
    assert invocation.selected_agent.agent_id == "coding-agent"
    assert invocation.status == "completed"
    assert context.active_tasks == [payload["task_ids"]["code/repository_inspection"]]


def test_pi_write_risk_coding_flow_requires_confirmation_without_execution() -> None:
    asyncio.run(_pi_write_risk_coding_flow_requires_confirmation_without_execution())


async def _pi_write_risk_coding_flow_requires_confirmation_without_execution() -> None:
    coding_card = load_agent_card(CODING_CARD)
    coding_app = create_coding_a2a_app(coding_card)
    directory = DirectoryClient(fake_ads=True)
    state_store = InMemoryStateStore()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=coding_app),
        base_url=coding_card.url,
    ) as coding_http_client:
        transport = HTTPA2ATransport(
            http_clients_by_origin={_origin(coding_card.url): coding_http_client}
        )
        spy_client = SpyA2AClient(A2AClient(transport))
        pi_app = create_app(
            directory_client=directory,
            a2a_client=spy_client,
            state_store=state_store,
            publish_weather_card=False,
            publish_coding_card=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pi_app),
            base_url="http://pi-agent",
        ) as pi_http_client:
            response = await pi_http_client.post(
                "/messages",
                json={"user_id": "user-1", "channel": "api", "text": WRITE_RISK_PROMPT},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert spy_client.calls == []
    assert payload["workflow"] == ["code/bug_fix"]
    assert payload["selected_agents"] == [
        {"skill": "code/bug_fix", "agent_id": "coding-agent", "task_id": None}
    ]
    assert payload["task_id"] is None
    assert payload["task_ids"] == {}
    assert payload["artifacts"] == []
    assert payload["confirmation_required"] is True
    assert payload["policy_decision"] == "requires_confirmation"
    assert payload["confirmation_status"] == "pending"
    assert payload["pending_confirmation"]["skill"] == "code/bug_fix"
    assert payload["proposed_action"]["modifies_repository"] is True
    assert payload["risk_level"] == "medium"

    context = state_store.get_context(payload["context_id"])
    assert context.active_tasks == []


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"
