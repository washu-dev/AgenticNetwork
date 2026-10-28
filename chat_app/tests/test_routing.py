from __future__ import annotations

from agentnet.models import AgentCapability, DiscoveredAgent
from chat_app.planner.routing import rank_agents, score_agents


def _agent(agent_id: str, name: str, description: str, skill: str, tags: list[str]) -> DiscoveredAgent:
    return DiscoveredAgent(
        agent_id=agent_id,
        name=name,
        organization="test",
        description=description,
        skills=[AgentCapability(name=skill, description=description, skill_tags=tags)],
        endpoint=f"http://{agent_id}.test",
        source_record_id=agent_id,
    )


EMAIL = _agent("email-agent", "Email Agent", "Send and read Gmail messages.", "Send email", ["email/send"])
WEATHER = _agent("weather-agent", "Weather Agent", "Forecasts for any city.", "Forecast", ["weather/forecast"])
CODING = _agent("coding-agent", "Coding Agent", "Writes and runs Python programs.", "Write code", ["code/python"])


def test_best_match_first() -> None:
    agents = [EMAIL, WEATHER, CODING]
    assert rank_agents("what's the weather forecast in Chicago tomorrow?", agents)[0] is WEATHER
    assert rank_agents("write a python program", agents)[0] is CODING
    assert rank_agents("email these notes to Sam", agents)[0] is EMAIL


def test_plural_and_case_fold() -> None:
    assert rank_agents("Send EMAILS", [WEATHER, EMAIL])[0] is EMAIL


def test_empty_or_stopword_query_keeps_directory_order() -> None:
    agents = [CODING, EMAIL, WEATHER]
    assert rank_agents("", agents) == agents
    assert rank_agents("can you help me with this?", agents) == agents


def test_ties_keep_directory_order_and_every_agent_is_returned() -> None:
    agents = [CODING, EMAIL, WEATHER]
    ranked = rank_agents("translate this poem", agents)
    assert ranked == agents
    assert rank_agents("forecast", agents) == [WEATHER, CODING, EMAIL]


def test_accented_words_stay_whole() -> None:
    translator = _agent("translator", "Traductor", "Traducir al español.", "Traducir", ["text/translate"])
    assert rank_agents("traducir al español", [EMAIL, translator])[0] is translator
    assert {s for _, s in score_agents("español", [translator])} == {1}


def test_no_keyword_match_scores_every_agent_zero() -> None:
    assert {s for _, s in score_agents("translate this poem", [EMAIL, WEATHER])} == {0}
    assert {s for _, s in score_agents("", [EMAIL, WEATHER])} == {0}
