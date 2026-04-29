from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, Field

from v2_agent_network.services.common.models import AgentCapability, DiscoveredAgent
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.router import ALL_KNOWN_SKILLS


class AvailableSkill(BaseModel):
    skill: str
    agent_id: str
    agent_name: str
    description: str
    risk_level: str
    action_type: str
    input_modes: list[str] = Field(default_factory=list)
    output_modes: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


def build_skill_catalog(
    directory_client: DirectoryClient,
    skill_hints: Iterable[str] | None = None,
) -> list[AvailableSkill]:
    """Build a planner-safe skill catalog from DirectoryClient search results.

    ADS has no portable global capability listing in this implementation, so the
    real backend enumerates by searching known skill hints and post-filtering
    the pulled records. The result is still DirectoryClient-backed discovery;
    it does not load local Agent Cards or fake records in real ADS mode.
    """

    catalog: dict[tuple[str, str], AvailableSkill] = {}
    for agent in directory_client.search_agents():
        for capability in agent.skills:
            for skill in _skill_ids_for_capability(capability):
                catalog[(skill, agent.agent_id)] = _available_skill(agent, capability, skill)

    hints = _dedupe([*(skill_hints or ()), *ALL_KNOWN_SKILLS])
    for skill in hints:
        if not skill:
            continue
        for agent in directory_client.search_agents(skill=skill):
            for capability in agent.skills:
                if not _capability_supports(capability, skill):
                    continue
                catalog[(skill, agent.agent_id)] = _available_skill(agent, capability, skill)
    return list(catalog.values())


def _available_skill(
    agent: DiscoveredAgent,
    capability: AgentCapability,
    skill: str,
) -> AvailableSkill:
    return AvailableSkill(
        skill=skill,
        agent_id=agent.agent_id,
        agent_name=agent.name,
        description=capability.description or agent.description,
        risk_level=capability.risk_level.value,
        action_type=capability.action_type,
        input_modes=capability.input_modes or agent.input_modes,
        output_modes=capability.output_modes or agent.output_modes,
        examples=capability.examples,
    )


def _capability_supports(capability: AgentCapability, skill: str) -> bool:
    official_names = {item.name for item in capability.official_oasf_skills}
    return skill in (
        set(capability.skill_tags) | set(capability.custom_skill_tags) | official_names
    )


def _skill_ids_for_capability(capability: AgentCapability) -> list[str]:
    return _dedupe(
        [
            *capability.skill_tags,
            *capability.custom_skill_tags,
            *(item.name for item in capability.official_oasf_skills),
        ]
    )


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
