"""Canonical Agent Card → OASF/ADS record converter.

Single source of truth for all agents. Drawn from
agent_directory_service/services/directory/card_oasf_converter.py — if that
file changes, update this one to match.

Only stdlib + a2a.types dependencies so this module installs into any agent
environment without extra packages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from a2a.types import AgentCard


# ── Official OASF skill taxonomy entries known to this network ────────────────

@dataclass
class OfficialOASFSkill:
    name: str
    id: int | None = None


KNOWN_OASF_SKILLS: dict[str, OfficialOASFSkill] = {
    "images_computer_vision/image_classification": OfficialOASFSkill(
        name="images_computer_vision/image_classification",
        id=203,
    )
}

# Official OASF taxonomy skill entries sourced from:
# https://github.com/agntcy/oasf/tree/main/schema/skills
# Full path = directory path under schema/skills/ + the "name" field in each JSON file.
# IDs follow the pattern: parent_category_id * 100 + skill uid.

EMAIL_ADS_ROUTING_SKILLS: list[OfficialOASFSkill] = [
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

# Official OASF routing skills per agent, keyed by lowercased hyphenated card name.
# Used when the card's skill IDs have no entry in KNOWN_OASF_SKILLS.
_AGENT_OASF_ROUTING_SKILLS: dict[str, list[OfficialOASFSkill]] = {
    "coding-agent": [
        OfficialOASFSkill(name="analytical_skills/coding_skills/text_to_code", id=50201),
        OfficialOASFSkill(name="analytical_skills/coding_skills/code_optimization", id=50204),
    ],
    "intelligence-agent": [
        OfficialOASFSkill(
            name="natural_language_processing/information_retrieval_synthesis/knowledge_synthesis",
            id=10303,
        ),
        OfficialOASFSkill(
            name="natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search",
            id=10306,
        ),
        OfficialOASFSkill(
            name="natural_language_processing/natural_language_generation/summarization",
            id=10202,
        ),
    ],
    "disaster-response-agent": [
        OfficialOASFSkill(name="advanced_reasoning_planning/strategic_planning", id=1501),
        OfficialOASFSkill(
            name="natural_language_processing/information_retrieval_synthesis/knowledge_synthesis",
            id=10303,
        ),
    ],
    "geospatial-agent": [
        OfficialOASFSkill(name="images_computer_vision/image_classification", id=203),
        OfficialOASFSkill(name="images_computer_vision/object_detection", id=204),
    ],
}

WRITE_SKILL_IDS: set[str] = {
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

WRITE_SKILL_TERMS: set[str] = {
    "append", "bug_fix", "create", "delete", "group_send",
    "replace", "send", "test_fix", "update", "write",
}


# ── Public API ────────────────────────────────────────────────────────────────

def load_agent_card(path: str | Path) -> AgentCard:
    with Path(path).open("r", encoding="utf-8") as fh:
        return AgentCard.model_validate(json.load(fh))


def agent_card_to_oasf_record(card: AgentCard) -> dict[str, Any]:
    """Convert an AgentCard to an internal OASF record (demo-v2 schema)."""
    _validate(card)
    official, custom = _official_and_custom_skills(card)
    provider = card.provider.organization if card.provider else "unknown"
    record_id = str(uuid5(NAMESPACE_URL, f"agent-directory-service:{card.name}:{card.version}"))
    skill_policies = {skill.id: _skill_policy(skill) for skill in card.skills}
    has_write = any(p["action_type"] == "write" for p in skill_policies.values())

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
            "risk_level": "medium" if has_write else "low",
            "action_type": "write" if has_write else "read",
            "write_requires_confirmation": True,
            "skill_policies": skill_policies,
        },
        "metadata": {
            "source": "agent_directory_service",
            "demo_specific_custom_skills": custom,
        },
    }


def agent_card_to_ads_record(card: AgentCard) -> dict[str, Any]:
    """Convert an AgentCard to an ADS-native record (schema_version 1.0.0)."""
    _validate(card)
    base = agent_card_to_oasf_record(card)
    provider_url = card.provider.url if card.provider and card.provider.url else ""
    agent_id = base["agent_id"]
    routing_skills = _ads_routing_skills(card)
    custom_tags = base["discovery"]["custom_skill_tags"]
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "schema_version": "1.0.0",
        "name": _ads_name(provider_url, agent_id),
        "version": card.version,
        "authors": [card.provider.organization if card.provider else "AgenticNetwork"],
        "description": card.description,
        "created_at": created_at,
        "skills": [_ads_skill_payload(s) for s in routing_skills],
        "domains": [],
        "locators": [
            {
                "type": "source_code",
                "urls": [provider_url or card.url],
                "annotations": {"a2a.url": card.url},
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
            "agentic_network.record_id": base["record_id"],
            "agentic_network.display_name": card.name,
            "agentic_network.organization": base.get("organization", "unknown"),
            "agentic_network.custom_skill_tags": ",".join(custom_tags),
        },
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _validate(card: AgentCard) -> None:
    if not card.name:
        raise ValueError("AgentCard requires name")
    if not card.url:
        raise ValueError("AgentCard requires url")
    if not card.skills:
        raise ValueError("AgentCard requires at least one skill")


def _skill_tags(card: AgentCard) -> list[str]:
    tags: list[str] = []
    for skill in card.skills:
        tags.append(skill.id)
        tags.extend(skill.tags or [])
    return sorted(set(tags))


def _official_and_custom_skills(card: AgentCard) -> tuple[list[dict[str, Any]], list[str]]:
    official: list[dict[str, Any]] = []
    custom: list[str] = []
    for tag in _skill_tags(card):
        if tag in KNOWN_OASF_SKILLS:
            s = KNOWN_OASF_SKILLS[tag]
            official.append({"name": s.name, "id": s.id})
        else:
            custom.append(tag)
    return official, custom


def _skill_policy(skill: Any) -> dict[str, Any]:
    tags = set(skill.tags or [])
    is_write = (
        skill.id in WRITE_SKILL_IDS
        or "write" in tags
        or _looks_like_write(skill)
    )
    return {
        "risk_level": "medium" if is_write else "low",
        "action_type": "write" if is_write else "read",
        "confirmation_required": is_write,
    }


def _looks_like_write(skill: Any) -> bool:
    values = {str(skill.id).lower(), *(str(t).lower() for t in (skill.tags or []))}
    description = str(getattr(skill, "description", "") or "").lower()
    name = str(getattr(skill, "name", "") or "").lower()
    for term in WRITE_SKILL_TERMS:
        term_text = term.replace("_", " ")
        if (
            any(term in v for v in values)
            or term_text in description
            or term_text in name
        ):
            return True
    return False


def _ads_routing_skills(card: AgentCard) -> list[OfficialOASFSkill]:
    official = [
        KNOWN_OASF_SKILLS[skill.id]
        for skill in card.skills
        if skill.id in KNOWN_OASF_SKILLS
    ]
    if card.name.lower().replace(" ", "-") == "email-agent":
        official.extend(EMAIL_ADS_ROUTING_SKILLS)
    deduped: dict[str, OfficialOASFSkill] = {}
    for s in official:
        deduped[s.name] = s
    # If no official OASF taxonomy entry matched the card's skill IDs, use the
    # per-agent routing skill map. Routing skills are only for ADS discoverability;
    # the planner matches agents by reading the full card stored in annotations.
    if not deduped:
        agent_key = card.name.lower().replace(" ", "-")
        for s in _AGENT_OASF_ROUTING_SKILLS.get(agent_key, []):
            deduped[s.name] = s
    if not deduped:
        raise ValueError(
            f"No OASF routing skills found for agent '{card.name}'. "
            "Add an entry to _AGENT_OASF_ROUTING_SKILLS in ads_utils/converter.py."
        )
    return list(deduped.values())


def _ads_name(provider_url: str, agent_id: str) -> str:
    if provider_url.startswith(("http://", "https://")):
        return provider_url.rstrip("/")
    return f"https://example.invalid/agents/{agent_id}"


def _ads_skill_payload(skill: OfficialOASFSkill) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": skill.name}
    if skill.id is not None:
        payload["id"] = skill.id
    return payload
