from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid5, NAMESPACE_URL

from a2a.types import AgentCard

from planner_agent.main_agent.models import (
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

# Official OASF routing hints used for real ADS publication when a card only
# exposes project-specific custom skill tags. These are not treated as aliases
# for the custom skills; PI still post-filters by the card's custom skill tags
# before invoking the A2A endpoint.
EMAIL_ADS_ROUTING_SKILLS = [
    OfficialOASFSkill(
        name="natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search",
        id=10306,
    ),
    OfficialOASFSkill(
        name="natural_language_processing/natural_language_generation/summarization",
        id=10202,
    ),
    OfficialOASFSkill(
        name="retrieval_augmented_generation/retrieval_of_information/document_retrieval",
        id=60103,
    ),
]

WRITE_SKILL_IDS = {
    "email/send",
    "email/calendar/create_event",
    "email/docs/create",
    "email/docs/append",
    "email/docs/replace_text",
    "email/docs/create_and_email",
    "email/sheets/create",
    "email/sheets/write",
    "email/sheets/append",
    "calendar/create_event",
    "calendar/update_event",
    "calendar/delete_event",
    "code/bug_fix",
    "code/test_generation",
}
WRITE_SKILL_TERMS = {
    "append",
    "bug_fix",
    "create",
    "delete",
    "group_send",
    "replace",
    "send",
    "test_fix",
    "update",
    "write",
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
    record_id = str(uuid5(NAMESPACE_URL, f"planner-agent:{card.name}:{card.version}"))
    skill_policies = {skill.id: _skill_policy(skill) for skill in card.skills}
    has_write_skill = any(policy["action_type"] == "write" for policy in skill_policies.values())

    return {
        "schema_version": "demo-v3",
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
            "risk_level": "medium" if has_write_skill else "low",
            "action_type": "write" if has_write_skill else "read",
            "write_requires_confirmation": True,
            "skill_policies": skill_policies,
        },
        "metadata": {
            "source": "planner_agent",
            "demo_specific_custom_skills": custom,
        },
    }


def agent_card_to_ads_record(card: AgentCard) -> dict[str, Any]:
    """Build an ADS/OASF-shaped record for real Directory publication.

    The record keeps A2A interaction details separate from ADS discovery fields.
    Official OASF skills are used only for ADS routing hints where known; the
    card's specific skills remain custom tags under metadata and are used by PI
    for post-filtering and policy/A2A invocation.
    """
    base_record = agent_card_to_oasf_record(card)
    provider_url = card.provider.url if card.provider and card.provider.url else ""
    agent_id = base_record["agent_id"]
    name = _ads_record_name(provider_url, agent_id)
    routing_skills = _ads_routing_skills_for_card(card)
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    custom_tags = base_record["discovery"]["custom_skill_tags"]

    return {
        "schema_version": "1.0.0",
        "name": name,
        "version": card.version,
        "authors": [card.provider.organization if card.provider else "AgenticNetwork"],
        "description": card.description,
        "created_at": created_at,
        "skills": [_ads_skill_payload(skill) for skill in routing_skills],
        "domains": [],
        "locators": [
            {
                "type": "source_code",
                "urls": [provider_url or card.url],
                "annotations": {
                    "a2a.url": card.url,
                },
            }
        ],
        "modules": [
            {
                "name": "integration/a2a",
                "data": {
                    "card_schema_version": card.protocol_version or "0.3.0",
                    "card_data": card.model_dump(mode="json", by_alias=True, exclude_none=True),
                },
            }
        ],
        "annotations": {
            "a2a.url": card.url,
            "agentic_network.agent_id": agent_id,
            "agentic_network.record_id": base_record["record_id"],
            "agentic_network.display_name": card.name,
            "agentic_network.organization": base_record.get("organization", "unknown"),
            "agentic_network.custom_skill_tags": ",".join(custom_tags),
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
    skill_policies = record.get("policy", {}).get("skill_policies", {})

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
            risk_level=RiskLevel(
                skill_policies.get(skill.id, {}).get(
                    "risk_level",
                    record.get("policy", {}).get("risk_level", "low"),
                )
            ),
            examples=skill.examples or [],
            official_oasf_skills=official,
            custom_skill_tags=_custom_tags_for_skill(skill),
            action_type=skill_policies.get(skill.id, {}).get(
                "action_type",
                record.get("policy", {}).get("action_type", "read"),
            ),
        )
        for skill in card.skills
    ]

    return DiscoveredAgent(
        agent_id=record["agent_id"],
        name=card.name,
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


def published_record_from_oasf(
    record: dict[str, Any],
    backend: str = "fake",
    backend_ref: str | None = None,
) -> PublishedRecord:
    return PublishedRecord(
        record_id=record["record_id"],
        agent_id=record["agent_id"],
        backend=backend,
        raw_record=record,
        backend_ref=backend_ref,
    )


def _skill_policy(skill: Any) -> dict[str, Any]:
    tags = set(skill.tags or [])
    is_write = (
        skill.id in WRITE_SKILL_IDS
        or "write" in tags
        or _looks_like_write_skill(skill)
    )
    return {
        "risk_level": "medium" if is_write else "low",
        "action_type": "write" if is_write else "read",
        "confirmation_required": is_write,
    }


def _custom_tags_for_skill(skill: Any) -> list[str]:
    tags = [skill.id, *(skill.tags or [])]
    return sorted({tag for tag in tags if tag not in KNOWN_OASF_SKILLS})


def _looks_like_write_skill(skill: Any) -> bool:
    values = {str(skill.id).lower(), *(str(tag).lower() for tag in (skill.tags or []))}
    description = str(getattr(skill, "description", "") or "").lower()
    name = str(getattr(skill, "name", "") or "").lower()
    for term in WRITE_SKILL_TERMS:
        term_text = term.replace("_", " ")
        if (
            any(term in value for value in values)
            or term_text in description
            or term_text in name
        ):
            return True
    return False


def _ads_record_name(provider_url: str, agent_id: str) -> str:
    if provider_url.startswith(("http://", "https://")):
        return provider_url.rstrip("/")
    return f"https://example.invalid/agents/{agent_id}"


def _ads_routing_skills_for_card(card: AgentCard) -> list[OfficialOASFSkill]:
    official = [
        KNOWN_OASF_SKILLS[skill.id]
        for skill in card.skills
        if skill.id in KNOWN_OASF_SKILLS
    ]
    if card.name.lower().replace(" ", "-") == "email-agent":
        official.extend(EMAIL_ADS_ROUTING_SKILLS)
    deduped: dict[str, OfficialOASFSkill] = {}
    for skill in official:
        deduped[skill.name] = skill
    return list(deduped.values())


def _ads_skill_payload(skill: OfficialOASFSkill) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": skill.name}
    if skill.id is not None:
        payload["id"] = skill.id
    return payload
