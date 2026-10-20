from __future__ import annotations

import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

from agentnet.directory.card_oasf import (
    _oasf_skill_from_tag,
    agent_card_to_ads_record,
    agent_card_to_oasf_record,
    oasf_record_to_discovered_agent,
)
from agentnet.directory.client import DirectoryClient
from agentnet.models import (
    A2AMessage,
    A2APart,
    A2ATaskRequest,
)


def test_internal_a2a_model_validation() -> None:
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


def _card_with_skill_tags(tags: list[str]) -> AgentCard:
    return AgentCard(
        name="Weather Bridge Agent",
        description="Generator-produced wrapper agent.",
        url="http://127.0.0.1:19111/",
        provider=AgentProvider(organization="Demo", url="https://example.invalid/agents/weather"),
        version="0.1.0",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        defaultInputModes=["text/plain"],
        defaultOutputModes=["application/json"],
        skills=[
            AgentSkill(
                id="weather-bridge-agent/get-forecast",
                name="Get Forecast",
                description="Return the forecast for a place.",
                tags=tags,
                inputModes=["text/plain"],
                outputModes=["application/json"],
            )
        ],
    )


def test_ads_record_routes_generated_card_via_on_card_oasf_tag() -> None:
    # A generated wrapper agent declares its own routing skill as an oasf:<name>:<id>
    # tag; the converter must surface it even though the card has no entry in
    # KNOWN_OASF_SKILLS or the per-agent routing map. Only the id is sent: ADS
    # resolves the name from its taxonomy.
    card = _card_with_skill_tags(
        ["weather", "oasf:natural_language_processing/.../question_answering:10302"]
    )
    record = agent_card_to_ads_record(card)
    assert record["skills"] == [{"id": 10302}]


def test_ads_record_unions_on_card_tag_with_known_skill() -> None:
    # An explicit author oasf: tag is unioned with (never silently dropped by) a
    # KNOWN_OASF_SKILLS match on another skill id.
    card = _card_with_skill_tags(["oasf:analytical_skills/coding_skills/text_to_code:50201"])
    card.skills.append(
        AgentSkill(
            id="images_computer_vision/image_classification",
            name="Classify",
            description="Classify an image.",
            tags=[],
        )
    )
    record = agent_card_to_ads_record(card)
    # Both routing skills, sent by id only: text_to_code (50201) and image_classification (203).
    assert sorted(record["skills"], key=lambda skill: skill["id"]) == [{"id": 203}, {"id": 50201}]


def test_ads_record_backward_compatible_without_oasf_tag() -> None:
    # Existing cards carry no oasf: tag and an unknown skill id -> no routing skills,
    # exactly as before the fallback was added.
    card = _card_with_skill_tags(["weather", "demo-custom"])
    assert agent_card_to_ads_record(card)["skills"] == []


def test_oasf_skill_from_tag_parses_and_rejects() -> None:
    parsed = _oasf_skill_from_tag("oasf:nlp/retrieval/document_retrieval:60103")
    assert parsed is not None
    assert parsed.name == "nlp/retrieval/document_retrieval"
    assert parsed.id == 60103
    # Non-oasf tag, missing id, and non-integer id are all ignored.
    assert _oasf_skill_from_tag("weather/forecast") is None
    assert _oasf_skill_from_tag("oasf:nlp/no_id") is None
    assert _oasf_skill_from_tag("oasf:nlp/bad:notanint") is None
    assert _oasf_skill_from_tag("oasf::123") is None
