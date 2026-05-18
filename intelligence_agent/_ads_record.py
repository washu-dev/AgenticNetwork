"""Self-contained Agent Card -> ADS record (OASF schema 1.0.0) converter.

This is the publish-path converter the intelligence agent needs to register
itself with a real ADS via ``dirctl push``. It is intentionally inlined here
so the external intelligence-agent service is self-contained at runtime.

Only the subset required for ``agent_card_to_ads_record`` (and
``load_agent_card``) is reproduced.

OASF taxonomy
-------------
The ADS server validates ``skills[].name`` / ``skills[].id`` and
``modules[].id`` against the OASF taxonomy at https://schema.oasf.outshift.com.
Custom strings are rejected, so this file maps the intelligence agent's card
skills onto official OASF analytical/NLP skill classes. The mapping is
intentionally broad — PI-side discovery still post-filters by
``annotations.agentic_network.custom_skill_tags``, which carries the precise
``intelligence/*`` skill tags from the Agent Card.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from a2a.types import AgentCard


@dataclass(frozen=True)
class _OASFSkill:
    name: str
    id: int


# OASF skill ids sourced from scout_agent's canonical mapping (which traces
# back to https://github.com/agntcy/oasf/tree/main/schema/skills). The
# intelligence agent claims analytical-reasoning + knowledge-synthesis
# leaves; situation summaries are summarization; threat assessment is
# inference/deduction over peer data.
_INTEL_ADS_ROUTING_SKILLS: list[_OASFSkill] = [
    _OASFSkill(
        name="natural_language_processing/analytical_reasoning/inference_deduction",
        id=10701,
    ),
    _OASFSkill(
        name="natural_language_processing/information_retrieval_synthesis/knowledge_synthesis",
        id=10303,
    ),
    _OASFSkill(
        name="natural_language_processing/information_retrieval_synthesis/fact_extraction",
        id=10301,
    ),
    _OASFSkill(
        name="natural_language_processing/natural_language_generation/summarization",
        id=10202,
    ),
]

# OASF module class id for ``integration/a2a`` (from /api/modules).
_MODULE_INTEGRATION_A2A_ID = 203


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


def _ads_record_name(provider_url: str, agent_id: str) -> str:
    if provider_url.startswith(("http://", "https://")):
        return provider_url.rstrip("/")
    return f"https://example.invalid/agents/{agent_id}"


def _ads_skill_payload(skill: _OASFSkill) -> dict[str, Any]:
    return {"name": skill.name, "id": skill.id}


def agent_card_to_ads_record(card: AgentCard) -> dict[str, Any]:
    """Build an ADS-publishable record (schema 1.0.0) from an A2A Agent Card.

    The record carries the full Agent Card under ``modules[].data.card_data`` so
    that consumers reading the record back can reconstruct the A2A endpoint.
    All custom skill tags from the card are joined into
    ``annotations.agentic_network.custom_skill_tags`` for client-side filtering;
    ``skills[]`` itself only carries OASF-validated taxonomy entries.
    """
    validate_agent_card(card)
    custom_tags = _skill_tags(card)
    provider = card.provider.organization if card.provider else "AgenticNetwork"
    provider_url = card.provider.url if card.provider and card.provider.url else ""
    agent_id = card.name.lower().replace(" ", "-")
    record_id = str(uuid5(NAMESPACE_URL, f"v2-agent-network:{card.name}:{card.version}"))
    name = _ads_record_name(provider_url, agent_id)
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "schema_version": "1.0.0",
        "name": name,
        "version": card.version,
        "authors": [provider],
        "description": card.description,
        "created_at": created_at,
        "skills": [_ads_skill_payload(skill) for skill in _INTEL_ADS_ROUTING_SKILLS],
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
                "id": _MODULE_INTEGRATION_A2A_ID,
                "data": {
                    "card_schema_version": card.protocol_version or "0.3.0",
                    "card_data": card.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                },
            }
        ],
        "annotations": {
            "a2a.url": card.url,
            "agentic_network.agent_id": agent_id,
            "agentic_network.record_id": record_id,
            "agentic_network.display_name": card.name,
            "agentic_network.organization": provider,
            "agentic_network.custom_skill_tags": ",".join(custom_tags),
        },
    }
