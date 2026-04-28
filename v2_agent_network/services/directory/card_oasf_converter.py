from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid5, NAMESPACE_URL

from a2a.types import AgentCard

from v2_agent_network.services.common.models import (
    AgentCapability,
    DiscoveredAgent,
    OfficialOASFSkill,
    PublishedRecord,
    RiskLevel,
)


# Local known official mappings only. Demo-only skills are preserved as custom
# tags and must not be represented as official OASF taxonomy entries.
KNOWN_OASF_SKILLS: dict[str, OfficialOASFSkill] = {
    "images_computer_vision/image_classification": OfficialOASFSkill(
        name="images_computer_vision/image_classification",
        id=203,
    )
}


def load_agent_card(path: str | Path) -> AgentCard:
    with Path(path).open("r", encoding="utf-8") as fh:
        return AgentCard.model_validate(json.load(fh))


def validate_agent_card(card: AgentCard) -> None:
    if not card.name:
        raise ValueError("Agent Card requires name")
    if not card.url:
        raise ValueError("Agent Card requires url")
    if not card.skills:
        raise ValueError("Agent Card requires at least one skill")


def _skill_tags(card: AgentCard) -> list[str]:
    tags: list[str] = []
    for skill in card.skills:
        tags.append(skill.id)
        tags.extend(skill.tags)
    return sorted(set(tags))


def _official_and_custom_skills(card: AgentCard) -> tuple[list[dict[str, Any]], list[str]]:
    official: list[dict[str, Any]] = []
    custom: list[str] = []
    for tag in _skill_tags(card):
        if tag in KNOWN_OASF_SKILLS:
            official.append(KNOWN_OASF_SKILLS[tag].model_dump())
        else:
            custom.append(tag)
    return official, custom


def agent_card_to_oasf_record(card: AgentCard) -> dict[str, Any]:
    validate_agent_card(card)
    official, custom = _official_and_custom_skills(card)
    provider = card.provider.organization if card.provider else "unknown"
    record_id = str(uuid5(NAMESPACE_URL, f"v2-agent-network:{card.name}:{card.version}"))

    return {
        "schema_version": "demo-v2",
        "record_type": "agntcy.oasf.demo.agent_record",
        "record_id": record_id,
        "agent_id": card.name.lower().replace(" ", "-"),
        "name": card.name,
        "organization": provider,
        "description": card.description,
        "interaction": {
            "type": "a2a_agent_card",
            "endpoint": card.url,
            "preferred_transport": (card.preferred_transport or "JSONRPC").lower(),
            "agent_card": card.model_dump(mode="json", by_alias=True, exclude_none=True),
        },
        "discovery": {
            "official_oasf_skills": official,
            "custom_skill_tags": custom,
            "custom_skill_note": (
                "Custom skill tags are demo-specific when no local official "
                "OASF taxonomy mapping is known."
            ),
            "domains": sorted({tag.split("/")[0] for tag in custom if "/" in tag}),
            "input_modes": card.default_input_modes,
            "output_modes": card.default_output_modes,
        },
        "auth": {
            "security": card.security or [],
            "security_schemes": card.security_schemes or {},
            "required_scopes": [],
        },
        "policy": {
            "risk_level": "low",
            "action_type": "read",
            "write_requires_confirmation": True,
        },
        "metadata": {
            "source": "v2_agent_network",
            "demo_specific_custom_skills": custom,
        },
    }


def validate_oasf_record(record: dict[str, Any]) -> None:
    for field in ("record_id", "name", "interaction", "discovery"):
        if field not in record:
            raise ValueError(f"OASF record missing {field}")
    if not record["interaction"].get("agent_card"):
        raise ValueError("OASF record must preserve A2A Agent Card separately")


def oasf_record_to_agent_card(record: dict[str, Any]) -> AgentCard:
    validate_oasf_record(record)
    return AgentCard.model_validate(record["interaction"]["agent_card"])


def oasf_record_to_discovered_agent(record: dict[str, Any]) -> DiscoveredAgent:
    validate_oasf_record(record)
    card = oasf_record_to_agent_card(record)
    official = [
        OfficialOASFSkill.model_validate(skill)
        for skill in record["discovery"].get("official_oasf_skills", [])
    ]
    custom = record["discovery"].get("custom_skill_tags", [])

    capabilities = [
        AgentCapability(
            name=skill.name,
            description=skill.description,
            skill_tags=sorted(set([skill.id, *skill.tags])),
            domain_tags=record["discovery"].get("domains", []),
            input_modes=skill.input_modes or card.default_input_modes,
            output_modes=skill.output_modes or card.default_output_modes,
            required_auth=[],
            required_scopes=[],
            risk_level=RiskLevel(record.get("policy", {}).get("risk_level", "low")),
            examples=skill.examples or [],
            official_oasf_skills=official,
            custom_skill_tags=custom,
            action_type=record.get("policy", {}).get("action_type", "read"),
        )
        for skill in card.skills
    ]

    return DiscoveredAgent(
        agent_id=record["agent_id"],
        name=record["name"],
        organization=record.get("organization", "unknown"),
        description=record.get("description", ""),
        skills=capabilities,
        domains=record["discovery"].get("domains", []),
        input_modes=card.default_input_modes,
        output_modes=card.default_output_modes,
        endpoint=record["interaction"]["endpoint"],
        transport="http",
        auth=record.get("auth", {}),
        required_scopes=record.get("auth", {}).get("required_scopes", []),
        trust_level=0.7,
        source_record_id=record["record_id"],
        source_card_url=f"{record['interaction']['endpoint'].rstrip('/')}/.well-known/agent-card.json",
        raw_record=record,
        risk_level=RiskLevel(record.get("policy", {}).get("risk_level", "low")),
    )


def published_record_from_oasf(record: dict[str, Any], backend: str = "fake") -> PublishedRecord:
    return PublishedRecord(
        record_id=record["record_id"],
        agent_id=record["agent_id"],
        backend=backend,
        raw_record=record,
    )
