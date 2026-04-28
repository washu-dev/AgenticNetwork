from __future__ import annotations

from v2_agent_network.services.common.models import DiscoveredAgent, RoutingDecision
from v2_agent_network.services.directory.client import DirectoryClient


WEATHER_TERMS = {"weather", "forecast", "temperature", "rain", "wind", "sunny", "cloudy"}
WEATHER_SKILL = "weather/forecast"


def infer_skill(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in WEATHER_TERMS):
        return WEATHER_SKILL
    return WEATHER_SKILL


def select_agent(
    *,
    context_id: str,
    user_goal: str,
    directory_client: DirectoryClient,
) -> RoutingDecision:
    skill = infer_skill(user_goal)
    candidates = directory_client.search_agents(skill=skill)
    if not candidates:
        raise LookupError(f"No agent found for skill {skill}")
    selected = candidates[0]
    return RoutingDecision(
        context_id=context_id,
        user_goal=user_goal,
        selected_agents=[selected],
        rejected_agents=_rejected(candidates, selected),
        reason=f"Selected highest-ranked agent for skill {skill}.",
        confidence=0.9,
        requires_human_confirmation=False,
    )


def _rejected(candidates: list[DiscoveredAgent], selected: DiscoveredAgent) -> list[DiscoveredAgent]:
    return [agent for agent in candidates if agent.agent_id != selected.agent_id]

