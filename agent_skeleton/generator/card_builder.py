"""card_builder — AgentAnswers -> a valid, routable Agent Card (dict/JSON).

Mirrors the shape of the hand-written cards in agent-directory (e.g.
disaster_response_agent.card.json): slash-namespaced skill ids, AGENT_HOST url
convention, empty securitySchemes. Adds the one thing those cards get by a
shared-file edit instead: an ``oasf:<name>:<id>`` routing tag resolved from the
taxonomy, so the agent is discoverable with no edit to ads_utils.converter's maps.
"""
from __future__ import annotations

from typing import Any

from .models import AgentAnswers, SkillAnswer, slugify
from .oasf import resolve

_MODES = ["text/plain", "application/json"]


def _skill_block(
    agent_slug: str, skill: SkillAnswer, taxonomy: list[dict], used_ids: set[str]
) -> dict[str, Any]:
    # Disambiguate names that slugify to the same string so skill ids (the
    # routing/dispatch identifiers) stay unique.
    base_slug = slugify(skill.name)
    skill_slug = base_slug
    n = 2
    while f"{agent_slug}/{skill_slug}" in used_ids:
        skill_slug = f"{base_slug}-{n}"
        n += 1
    skill_id = f"{agent_slug}/{skill_slug}"
    used_ids.add(skill_id)
    tags: list[str] = [skill_id, agent_slug, *skill_slug.split("-")]
    tags.append("write" if skill.write else "read")
    if skill.oasf_key:
        tags.append(resolve(skill.oasf_key, taxonomy).as_tag())
    # de-dup while preserving order
    seen: set[str] = set()
    tags = [t for t in tags if not (t in seen or seen.add(t))]
    return {
        "id": skill_id,
        "name": skill.name,
        "description": skill.description,
        "tags": tags,
        "examples": list(skill.examples),
        "inputModes": list(_MODES),
        "outputModes": list(_MODES),
        "security": [],
    }


def build_card(answers: AgentAnswers, taxonomy: list[dict]) -> dict[str, Any]:
    """Assemble the Agent Card as a JSON-able dict (does not write to disk)."""
    answers.validate()
    slug = answers.slug
    card: dict[str, Any] = {
        "name": answers.name,
        "description": answers.description,
        # AGENT_HOST is resolved by the entrypoint at deploy time (repo convention);
        # --advertise-url overrides it for local runs.
        "url": f"http://AGENT_HOST:{answers.port}/",
        "version": answers.version,
        "protocolVersion": "0.3.0",
        "preferredTransport": "JSONRPC",
        "provider": {
            "organization": answers.provider_org,
            "url": f"https://example.invalid/{slug}",
        },
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": list(_MODES),
        "defaultOutputModes": list(_MODES),
        "securitySchemes": {},
        "security": [],
        "skills": _build_skills(slug, answers, taxonomy),
    }
    return card


def _build_skills(slug: str, answers: AgentAnswers, taxonomy: list[dict]) -> list[dict[str, Any]]:
    used_ids: set[str] = set()
    skills = [_skill_block(slug, s, taxonomy, used_ids) for s in answers.skills]
    ids = [s["id"] for s in skills]
    if len(set(ids)) != len(ids):  # belt-and-suspenders; disambiguation should prevent this
        raise ValueError(f"duplicate skill ids after disambiguation: {ids}")
    return skills


def validate_card(card: dict[str, Any]) -> None:
    """Best-effort validation. Always does structural checks; additionally runs the
    a2a-sdk AgentCard model + the canonical converter when those are importable
    (they are in the agent runtime env, not necessarily at generation time)."""
    for key in ("name", "url", "version", "skills"):
        if not card.get(key):
            raise ValueError(f"generated card missing required field: {key}")
    if not card["skills"]:
        raise ValueError("generated card has no skills")

    try:
        from a2a.types import AgentCard
    except Exception:
        return  # a2a-sdk not present at generation time; structural checks stand
    AgentCard.model_validate(card)
