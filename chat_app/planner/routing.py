"""Routing: which discovered agents the planner's model sees first for a task.

The model still makes the final choice of agent. When ``list_agents`` gets a
query, ``rank_agents`` orders the directory's agents by how well their cards
match it, and the runtime shows only the top ``SHORTLIST_SIZE`` (with a note
that more exist). Without a query, or when no card shares a keyword with it,
every agent is listed, as before.

Keyword overlap is a deliberate placeholder. A better routing algorithm
(retrieval over skills, embeddings, a ranker learned from traces) can replace
``rank_agents`` without touching the planner loop; see ``docs/PLANNER.md``.
"""
from __future__ import annotations

import re

from agentnet.models import DiscoveredAgent

# Past this many agents, a query returns a shortlist instead of every agent.
SHORTLIST_SIZE = 25

_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "about", "please",
        "can", "you", "your", "our", "are", "was", "will", "would", "could", "should",
        "what", "which", "who", "how", "when", "where", "why", "need", "want", "help",
        "agent", "agents", "task", "make", "get", "use", "using", "some", "any", "all",
    }
)


def _terms(text: str) -> set[str]:
    """Lowercase word set, minus stopwords and short words, with a naive plural fold."""
    terms: set[str] = set()
    for word in re.findall(r"[^\W_]+", text.lower()):
        if len(word) < 3 or word in _STOPWORDS:
            continue
        if len(word) > 4 and word.endswith("s"):
            word = word[:-1]
        terms.add(word)
    return terms


def _score(query_terms: set[str], agent: DiscoveredAgent) -> int:
    name = _terms(f"{agent.name} {agent.agent_id}")
    tags: set[str] = set()
    text = _terms(f"{agent.description} {' '.join(agent.domains)}")
    for cap in agent.skills:
        tags |= _terms(" ".join([cap.name, *cap.skill_tags, *cap.custom_skill_tags, *cap.domain_tags]))
        text |= _terms(" ".join([cap.description, *cap.examples]))
    return sum(3 * (t in name) + 2 * (t in tags) + (t in text) for t in query_terms)


def score_agents(query: str, agents: list[DiscoveredAgent]) -> list[tuple[DiscoveredAgent, int]]:
    """Return ``(agent, score)`` pairs, best match first; ties keep directory order.

    A query with no usable words scores every agent 0.
    """
    query_terms = _terms(query)
    scored = [(agent, _score(query_terms, agent) if query_terms else 0) for agent in agents]
    order = sorted(range(len(scored)), key=lambda i: (-scored[i][1], i))
    return [scored[i] for i in order]


def rank_agents(query: str, agents: list[DiscoveredAgent]) -> list[DiscoveredAgent]:
    """Return ``agents`` best match first; ties (and an empty query) keep directory order."""
    return [agent for agent, _ in score_agents(query, agents)]
