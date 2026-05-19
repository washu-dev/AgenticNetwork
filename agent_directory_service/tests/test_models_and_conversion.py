from __future__ import annotations

import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

from agent_directory_service.services.common.models import (
    A2AMessage,
    A2APart,
    A2ATaskRequest,
)
from agent_directory_service.services.directory.card_oasf_converter import (
    agent_card_to_oasf_record,
    load_agent_card,
    oasf_record_to_agent_card,
    oasf_record_to_discovered_agent,
)
from agent_directory_service.services.directory.client import DirectoryClient, DirectoryUnavailable


WEATHER_CARD = "agent_directory_service/configs/agent_cards/weather_agent.card.json"


def test_internal_a2a_model_validation_and_card_resolution() -> None:
    with pytest.raises(ValueError):
        A2APart(kind="text")

    request = A2ATaskRequest(
        context_id="context-1",
        target_agent_id="weather-agent",
        skill="weather/forecast",
        user_goal="weather in Austin",
        messages=[A2AMessage(role="user", parts=[A2APart(kind="text", text="weather")])],
    )
    assert request.context_id == "context-1"

    card = load_agent_card(WEATHER_CARD)
    agent = oasf_record_to_discovered_agent(agent_card_to_oasf_record(card))
    assert agent.agent_id == "weather-agent"
    assert agent.endpoint == card.url


def test_agent_card_and_oasf_conversion_keeps_weather_skill_custom() -> None:
    card = load_agent_card(WEATHER_CARD)
    record = agent_card_to_oasf_record(card)

    assert record["interaction"]["type"] == "a2a_agent_card"
    assert record["interaction"]["agent_card"]["name"] == "Weather Agent"
    assert record["discovery"]["official_oasf_skills"] == []
    assert "weather/forecast" in record["discovery"]["custom_skill_tags"]
    assert "weather/forecast" in record["metadata"]["demo_specific_custom_skills"]
    assert record["auth"]["security"] == []
    assert record["auth"]["required_scopes"] == []
    assert record["policy"]["risk_level"] == "low"
    assert record["policy"]["action_type"] == "read"

    round_trip_card = oasf_record_to_agent_card(record)
    discovered = oasf_record_to_discovered_agent(record)
    assert round_trip_card.name == card.name
    assert discovered.endpoint == card.url
    assert discovered.skills[0].custom_skill_tags


def test_fake_directory_publish_search_resolve_and_fake_gate(monkeypatch) -> None:
    card = load_agent_card(WEATHER_CARD)
    directory = DirectoryClient(fake_ads=True)
    published = directory.publish_agent_card(card)

    assert published.backend == "fake"
    assert published.backend_ref is None
    assert directory.search_agents(skill="weather/forecast")[0].agent_id == "weather-agent"
    assert directory.search_agents(skill="weather")[0].agent_id == "weather-agent"
    assert directory.resolve_agent("weather-agent").name == "Weather Agent"
    assert directory.resolve_agent(published.record_id).endpoint == card.url

    monkeypatch.delenv("ADS_SERVER_ADDR", raising=False)
    monkeypatch.delenv("ADS_URL", raising=False)
    monkeypatch.delenv("ADS_SERVER_ADDRESS", raising=False)
    monkeypatch.delenv("DIRECTORY_CLIENT_SERVER_ADDRESS", raising=False)
    real_directory = DirectoryClient(fake_ads=False)
    with pytest.raises(DirectoryUnavailable):
        real_directory.search_agents(skill="weather/forecast")


def test_directory_handles_official_oasf_skill_separately_from_custom_tags() -> None:
    card = AgentCard(
        name="Vision Agent",
        description="Official OASF image classification demo card.",
        url="http://127.0.0.1:9201/",
        provider=AgentProvider(organization="Demo", url="https://example.invalid"),
        version="0.1.0",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        defaultInputModes=["image/png"],
        defaultOutputModes=["application/json"],
        skills=[
            AgentSkill(
                id="images_computer_vision/image_classification",
                name="Image Classification",
                description="Classify image content.",
                tags=["demo-custom-vision-tag"],
                inputModes=["image/png"],
                outputModes=["application/json"],
            )
        ],
    )
    record = agent_card_to_oasf_record(card)
    assert record["discovery"]["official_oasf_skills"][0]["name"] == (
        "images_computer_vision/image_classification"
    )
    assert "demo-custom-vision-tag" in record["discovery"]["custom_skill_tags"]

    directory = DirectoryClient(fake_ads=True)
    directory.publish_oasf_record(record)
    official_result = directory.search_agents(skill="images_computer_vision/image_classification")
    custom_result = directory.search_agents(skill="demo-custom-vision-tag")
    assert official_result[0].name == "Vision Agent"
    assert custom_result[0].name == "Vision Agent"


def test_oasf_conversion_keeps_custom_tags_per_agent_card_skill_for_policy() -> None:
    card = AgentCard(
        name="Email Agent",
        description="Email agent with read and write custom skills.",
        url="http://127.0.0.1:9103/",
        provider=AgentProvider(organization="External Email", url="https://example.invalid"),
        version="0.2.0",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        defaultInputModes=["text/plain", "application/json"],
        defaultOutputModes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="email/search",
                name="Search Email",
                description="Search email metadata.",
                tags=["email/search", "email", "read"],
                inputModes=["text/plain", "application/json"],
                outputModes=["text/plain", "application/json"],
            ),
            AgentSkill(
                id="email/group_send",
                name="Send Group Email",
                description="Send one email to a group after confirmation.",
                tags=["email/group_send", "email/send", "email", "send", "write"],
                inputModes=["text/plain", "application/json"],
                outputModes=["text/plain", "application/json"],
            ),
        ],
    )

    record = agent_card_to_oasf_record(card)
    discovered = oasf_record_to_discovered_agent(record)
    search_capability = next(
        capability for capability in discovered.skills if "email/search" in capability.skill_tags
    )
    group_send_capability = next(
        capability
        for capability in discovered.skills
        if "email/group_send" in capability.skill_tags
    )

    assert "email/group_send" not in search_capability.custom_skill_tags
    assert "email/search" not in group_send_capability.custom_skill_tags
    assert search_capability.action_type == "read"
    assert group_send_capability.action_type == "write"
    assert group_send_capability.risk_level == "medium"
    assert record["policy"]["skill_policies"]["email/group_send"]["confirmation_required"] is True
