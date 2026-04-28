from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.server import (
    create_intelligence_a2a_app,
    create_photo_geospatial_a2a_app,
    create_weather_a2a_app,
)
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


PHOTO_CARD = "v2_agent_network/configs/agent_cards/photo_geospatial_agent.card.json"
INTELLIGENCE_CARD = "v2_agent_network/configs/agent_cards/intelligence_agent.card.json"
WEATHER_CARD = "v2_agent_network/configs/agent_cards/weather_agent.card.json"
PROMPT = "Analyze this geospatial photo and tomorrow's weather near Austin, then give me a situation summary and plan."


class SpyA2AClient(A2AClient):
    def __init__(self, wrapped: A2AClient):
        self.wrapped = wrapped
        self.calls: list[tuple[str, str]] = []
        self.requests_by_skill = {}

    async def send_task(self, agent, request):
        self.calls.append((agent.agent_id, request.skill))
        self.requests_by_skill[request.skill] = request
        return await self.wrapped.send_task(agent, request)


def test_photo_geospatial_agent_card_validation() -> None:
    card = load_agent_card(PHOTO_CARD)
    skill_ids = {skill.id for skill in card.skills}
    assert card.name == "Photo Geospatial Agent"
    assert "image/geospatial_analysis" in skill_ids
    assert "image/object_detection" in skill_ids
    assert "image/location_inference" in skill_ids
    assert "image/satellite_interpretation" in skill_ids
    assert "image/photo_forensics_basic" in skill_ids


def test_intelligence_agent_card_validation() -> None:
    card = load_agent_card(INTELLIGENCE_CARD)
    skill_ids = {skill.id for skill in card.skills}
    assert card.name == "Intelligence Agent"
    assert "intelligence/situation_summary" in skill_ids
    assert "intelligence/risk_assessment" in skill_ids
    assert "intelligence/course_of_action" in skill_ids
    assert "intelligence/entity_tracking" in skill_ids
    assert "intelligence/report_generation" in skill_ids


def test_oasf_conversion_for_photo_and_intelligence_cards_keeps_custom_skills() -> None:
    photo_record = agent_card_to_oasf_record(load_agent_card(PHOTO_CARD))
    intelligence_record = agent_card_to_oasf_record(load_agent_card(INTELLIGENCE_CARD))

    assert photo_record["discovery"]["official_oasf_skills"] == []
    assert intelligence_record["discovery"]["official_oasf_skills"] == []
    assert "image/geospatial_analysis" in photo_record["discovery"]["custom_skill_tags"]
    assert "intelligence/situation_summary" in intelligence_record["discovery"]["custom_skill_tags"]
    assert oasf_record_to_discovered_agent(photo_record).agent_id == "photo-geospatial-agent"
    assert oasf_record_to_discovered_agent(intelligence_record).agent_id == "intelligence-agent"


def test_fake_directory_publish_search_resolve_for_photo_and_intelligence() -> None:
    directory = DirectoryClient(fake_ads=True)
    photo_published = directory.publish_agent_card(load_agent_card(PHOTO_CARD))
    intelligence_published = directory.publish_agent_card(load_agent_card(INTELLIGENCE_CARD))

    assert directory.search_agents(skill="image/geospatial_analysis")[0].agent_id == (
        "photo-geospatial-agent"
    )
    assert directory.search_agents(skill="intelligence/situation_summary")[0].agent_id == (
        "intelligence-agent"
    )
    assert directory.search_agents(skill="intelligence/course_of_action")[0].agent_id == (
        "intelligence-agent"
    )
    assert directory.resolve_agent(photo_published.record_id).agent_id == "photo-geospatial-agent"
    assert directory.resolve_agent(intelligence_published.record_id).agent_id == "intelligence-agent"


def test_router_maps_3b_prompt_to_photo_weather_intelligence_workflow() -> None:
    assert plan_workflow(PROMPT) == [
        "image/geospatial_analysis",
        "weather/forecast",
        "intelligence/situation_summary",
    ]


def test_policy_allows_3b_read_only_skills() -> None:
    assert evaluate_skill("image/geospatial_analysis").status == "allowed"
    assert evaluate_skill("weather/forecast").status == "allowed"
    assert evaluate_skill("intelligence/situation_summary").status == "allowed"
    assert evaluate_skill("intelligence/course_of_action").status == "allowed"


def test_pi_geo_weather_intelligence_flow_over_a2a_http() -> None:
    asyncio.run(_pi_geo_weather_intelligence_flow_over_a2a_http())


async def _pi_geo_weather_intelligence_flow_over_a2a_http() -> None:
    photo_card = load_agent_card(PHOTO_CARD)
    weather_card = load_agent_card(WEATHER_CARD)
    intelligence_card = load_agent_card(INTELLIGENCE_CARD)
    photo_app = create_photo_geospatial_a2a_app(photo_card)
    weather_app = create_weather_a2a_app(weather_card)
    intelligence_app = create_intelligence_a2a_app(intelligence_card)
    directory = DirectoryClient(fake_ads=True)
    state_store = InMemoryStateStore()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=photo_app),
        base_url=photo_card.url,
    ) as photo_http_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=weather_app),
        base_url=weather_card.url,
    ) as weather_http_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=intelligence_app),
        base_url=intelligence_card.url,
    ) as intelligence_http_client:
        transport = HTTPA2ATransport(
            http_clients_by_origin={
                _origin(photo_card.url): photo_http_client,
                _origin(weather_card.url): weather_http_client,
                _origin(intelligence_card.url): intelligence_http_client,
            }
        )
        spy_client = SpyA2AClient(A2AClient(transport))
        pi_app = create_app(
            directory_client=directory,
            a2a_client=spy_client,
            state_store=state_store,
            publish_weather_card=True,
            publish_geo_intelligence_cards=True,
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
        ("photo-geospatial-agent", "image/geospatial_analysis"),
        ("weather-agent", "weather/forecast"),
        ("intelligence-agent", "intelligence/situation_summary"),
    ]
    assert payload["workflow"] == [
        "image/geospatial_analysis",
        "weather/forecast",
        "intelligence/situation_summary",
    ]
    assert payload["selected_agents"] == [
        {
            "skill": "image/geospatial_analysis",
            "agent_id": "photo-geospatial-agent",
            "task_id": payload["task_ids"]["image/geospatial_analysis"],
        },
        {
            "skill": "weather/forecast",
            "agent_id": "weather-agent",
            "task_id": payload["task_ids"]["weather/forecast"],
        },
        {
            "skill": "intelligence/situation_summary",
            "agent_id": "intelligence-agent",
            "task_id": payload["task_ids"]["intelligence/situation_summary"],
        },
    ]
    assert payload["confirmation_required"] is False
    assert payload["policy_decision"] == "allowed"
    assert payload["image_assessment"]["terrain"] == "mostly open terrain with a visible access route"
    assert payload["weather_forecast"]["location"] == "Austin"
    assert "situation_summary" in payload["situation_summary"]
    assert payload["key_risks"]
    assert payload["recommended_plan"]
    assert {artifact["artifact_id"] for artifact in payload["artifacts"]} == {
        "photo-geospatial-assessment",
        "weather-forecast",
        "intelligence-situation-summary",
    }

    intelligence_request = spy_client.requests_by_skill["intelligence/situation_summary"]
    assert "image/geospatial_analysis" in intelligence_request.metadata["previous_outputs"]
    assert "weather/forecast" in intelligence_request.metadata["previous_outputs"]
    assert intelligence_request.metadata["source_artifacts"] == [
        {
            "skill": "image/geospatial_analysis",
            "task_id": payload["task_ids"]["image/geospatial_analysis"],
            "artifact_id": "photo-geospatial-assessment",
            "name": "photo_geospatial_assessment",
        },
        {
            "skill": "weather/forecast",
            "task_id": payload["task_ids"]["weather/forecast"],
            "artifact_id": "weather-forecast",
            "name": "weather_forecast",
        },
    ]
    assert payload["structured_output"]["consumed_inputs"]["source_artifact_count"] == 2
    assert payload["structured_output"]["source_artifacts"] == intelligence_request.metadata[
        "source_artifacts"
    ]

    context = state_store.get_context(payload["context_id"])
    assert context.active_tasks == [
        payload["task_ids"]["image/geospatial_analysis"],
        payload["task_ids"]["weather/forecast"],
        payload["task_ids"]["intelligence/situation_summary"],
    ]
    for task_id in context.active_tasks:
        assert state_store.get_invocation(task_id).status == "completed"


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"

