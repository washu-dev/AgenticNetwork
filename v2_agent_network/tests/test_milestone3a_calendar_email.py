from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.server import create_calendar_a2a_app, create_email_a2a_app
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


CALENDAR_CARD = "v2_agent_network/configs/agent_cards/calendar_agent.card.json"
EMAIL_CARD = "v2_agent_network/configs/agent_cards/email_agent.card.json"
PROMPT = "Check my calendar tomorrow afternoon and draft an email to Bob with two meeting options."


class SpyA2AClient(A2AClient):
    def __init__(self, wrapped: A2AClient):
        self.wrapped = wrapped
        self.calls: list[tuple[str, str]] = []

    async def send_task(self, agent, request):
        self.calls.append((agent.agent_id, request.skill))
        return await self.wrapped.send_task(agent, request)


def test_calendar_agent_card_validation() -> None:
    card = load_agent_card(CALENDAR_CARD)
    skill_ids = {skill.id for skill in card.skills}
    assert card.name == "Calendar Agent"
    assert "calendar/search_availability" in skill_ids
    assert "calendar/create_event" in skill_ids
    assert "calendar/update_event" in skill_ids
    assert "calendar/respond_invitation" in skill_ids
    assert "calendar/summarize_schedule" in skill_ids


def test_email_agent_card_validation() -> None:
    card = load_agent_card(EMAIL_CARD)
    skill_ids = {skill.id for skill in card.skills}
    assert card.name == "Email Agent"
    assert "email/search" in skill_ids
    assert "email/read" in skill_ids
    assert "email/draft" in skill_ids
    assert "email/send" in skill_ids
    assert "email/summarize_thread" in skill_ids


def test_oasf_conversion_for_calendar_and_email_cards_keeps_custom_skills() -> None:
    calendar_record = agent_card_to_oasf_record(load_agent_card(CALENDAR_CARD))
    email_record = agent_card_to_oasf_record(load_agent_card(EMAIL_CARD))

    assert calendar_record["discovery"]["official_oasf_skills"] == []
    assert email_record["discovery"]["official_oasf_skills"] == []
    assert "calendar/search_availability" in calendar_record["discovery"]["custom_skill_tags"]
    assert "email/draft" in email_record["discovery"]["custom_skill_tags"]
    assert oasf_record_to_discovered_agent(calendar_record).agent_id == "calendar-agent"
    assert oasf_record_to_discovered_agent(email_record).agent_id == "email-agent"


def test_fake_directory_publish_search_resolve_for_calendar_and_email() -> None:
    directory = DirectoryClient(fake_ads=True)
    calendar_published = directory.publish_agent_card(load_agent_card(CALENDAR_CARD))
    email_published = directory.publish_agent_card(load_agent_card(EMAIL_CARD))

    assert directory.search_agents(skill="calendar/search_availability")[0].agent_id == "calendar-agent"
    assert directory.search_agents(skill="email/draft")[0].agent_id == "email-agent"
    assert directory.resolve_agent(calendar_published.record_id).agent_id == "calendar-agent"
    assert directory.resolve_agent(email_published.record_id).agent_id == "email-agent"


def test_router_maps_3a_prompt_to_calendar_then_email_workflow() -> None:
    assert plan_workflow(PROMPT) == ["calendar/search_availability", "email/draft"]
    assert plan_workflow("What is the weather in Austin?") == ["weather/forecast"]
    assert plan_workflow("Send this email to Bob.") == ["email/send"]


def test_policy_rules_for_calendar_and_email() -> None:
    assert evaluate_skill("calendar/search_availability").status == "allowed"
    assert evaluate_skill("email/draft").status == "allowed"
    assert evaluate_skill("email/send").status == "requires_confirmation"
    assert evaluate_skill("calendar/create_event").status == "requires_confirmation"
    assert evaluate_skill("calendar/update_event").status == "requires_confirmation"
    assert evaluate_skill("calendar/delete_event").status == "requires_confirmation"


def test_pi_multi_agent_flow_discovers_and_calls_calendar_then_email_over_a2a_http() -> None:
    asyncio.run(_pi_multi_agent_flow_discovers_and_calls_calendar_then_email_over_a2a_http())


async def _pi_multi_agent_flow_discovers_and_calls_calendar_then_email_over_a2a_http() -> None:
    calendar_card = load_agent_card(CALENDAR_CARD)
    email_card = load_agent_card(EMAIL_CARD)
    calendar_app = create_calendar_a2a_app(calendar_card)
    email_app = create_email_a2a_app(email_card)
    directory = DirectoryClient(fake_ads=True)
    state_store = InMemoryStateStore()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=calendar_app),
        base_url=calendar_card.url,
    ) as calendar_http_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=email_app),
        base_url=email_card.url,
    ) as email_http_client:
        transport = HTTPA2ATransport(
            http_clients_by_origin={
                _origin(calendar_card.url): calendar_http_client,
                _origin(email_card.url): email_http_client,
            }
        )
        spy_client = SpyA2AClient(A2AClient(transport))
        pi_app = create_app(
            directory_client=directory,
            a2a_client=spy_client,
            state_store=state_store,
            publish_weather_card=False,
            publish_calendar_email_cards=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pi_app),
            base_url="http://pi-agent",
        ) as pi_http_client:
            response = await pi_http_client.post(
                "/messages",
                json={"user_id": "user-1", "channel": "api", "text": PROMPT},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert spy_client.calls == [
        ("calendar-agent", "calendar/search_availability"),
        ("email-agent", "email/draft"),
    ]
    assert payload["workflow"] == ["calendar/search_availability", "email/draft"]
    assert payload["selected_agents"] == [
        {
            "skill": "calendar/search_availability",
            "agent_id": "calendar-agent",
            "task_id": payload["task_ids"]["calendar/search_availability"],
        },
        {
            "skill": "email/draft",
            "agent_id": "email-agent",
            "task_id": payload["task_ids"]["email/draft"],
        },
    ]
    assert len(payload["available_slots"]) == 2
    assert payload["available_slots"][0]["start"] == "tomorrow 1:30 PM"
    assert payload["draft_email"]["to"] == "bob@example.com"
    assert "tomorrow 1:30 PM" in payload["draft_email"]["body"]
    assert payload["confirmation_required"] is True
    assert payload["pending_confirmation"]["skill"] == "email/send"
    assert payload["policy_decision"] == "requires_confirmation"
    assert payload["confirmation_status"] == "pending"
    assert {artifact["artifact_id"] for artifact in payload["artifacts"]} == {
        "calendar-availability",
        "email-draft",
    }

    calendar_invocation = state_store.get_invocation(payload["task_ids"]["calendar/search_availability"])
    email_invocation = state_store.get_invocation(payload["task_ids"]["email/draft"])
    context = state_store.get_context(payload["context_id"])
    assert calendar_invocation.selected_agent.agent_id == "calendar-agent"
    assert email_invocation.selected_agent.agent_id == "email-agent"
    assert context.active_tasks == [
        payload["task_ids"]["calendar/search_availability"],
        payload["task_ids"]["email/draft"],
    ]


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"

