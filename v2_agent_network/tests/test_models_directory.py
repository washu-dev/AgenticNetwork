from __future__ import annotations

import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

from v2_agent_network.services.common.models import (
    A2AArtifact,
    A2APart,
    A2ATaskRequest,
    AgentInvocationRecord,
    Channel,
    ConfirmationStatus,
    PIContext,
    PolicyDecision,
    PolicyDecisionStatus,
    UserMessage,
)
from v2_agent_network.services.directory.card_oasf_converter import (
    agent_card_to_oasf_record,
    load_agent_card,
    oasf_record_to_agent_card,
    oasf_record_to_discovered_agent,
)
from v2_agent_network.services.directory.client import DirectoryClient, DirectoryUnavailable


WEATHER_CARD = "v2_agent_network/configs/agent_cards/weather_agent.card.json"


def test_internal_model_validation_and_invocation_record() -> None:
    with pytest.raises(ValueError):
        A2APart(kind="text")

    user_message = UserMessage(channel=Channel.api, sender_id="user-1", text="weather in Austin")
    context = PIContext(
        user_id="user-1",
        channel=Channel.api,
        original_message_id=user_message.id,
        current_goal=user_message.text,
    )
    request = A2ATaskRequest(
        context_id=context.context_id,
        target_agent_id="weather-agent",
        skill="weather/forecast",
        user_goal=user_message.text,
        messages=[],
    )
    assert request.context_id == context.context_id

    card = load_agent_card(WEATHER_CARD)
    agent = oasf_record_to_discovered_agent(agent_card_to_oasf_record(card))
    policy = PolicyDecision(
        status=PolicyDecisionStatus.allowed,
        reason="read-only",
        confirmation_status=ConfirmationStatus.not_required,
    )
    invocation = AgentInvocationRecord(
        context_id=context.context_id,
        task_id="task-1",
        agent_id=agent.agent_id,
        skill="weather/forecast",
        request_summary=user_message.text,
        status="completed",
        artifacts=[A2AArtifact(name="forecast", parts=[A2APart(kind="data", data={"ok": True})])],
        policy_decision=policy,
        selected_agent=agent,
    )
    assert invocation.policy_decision.status == PolicyDecisionStatus.allowed
    assert invocation.artifacts[0].parts[0].data == {"ok": True}


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


def test_fake_directory_publish_search_resolve_and_fake_gate() -> None:
    card = load_agent_card(WEATHER_CARD)
    directory = DirectoryClient(fake_ads=True)
    published = directory.publish_agent_card(card)

    assert published.backend == "fake"
    assert published.backend_ref is None
    assert directory.search_agents(skill="weather/forecast")[0].agent_id == "weather-agent"
    assert directory.search_agents(skill="weather")[0].agent_id == "weather-agent"
    assert directory.resolve_agent("weather-agent").name == "Weather Agent"
    assert directory.resolve_agent(published.record_id).endpoint == card.url

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
