from __future__ import annotations

from v2_agent_network.services.common.models import DiscoveredAgent, RoutingDecision
from v2_agent_network.services.directory.client import DirectoryClient


WEATHER_TERMS = {"weather", "forecast", "temperature", "rain", "wind", "sunny", "cloudy"}
CALENDAR_TERMS = {"calendar", "schedule", "availability", "available", "meeting options"}
EMAIL_TERMS = {"email", "mail", "message"}
IMAGE_GEO_TERMS = {"photo", "image", "geospatial", "satellite", "scene"}
INTELLIGENCE_TERMS = {"situation", "summary", "plan", "risk", "course of action"}
CODE_TERMS = {"repo", "repository", "code", "coding", "test", "tests", "bug", "fix", "pr", "pull request"}
WEATHER_SKILL = "weather/forecast"
CALENDAR_SEARCH_AVAILABILITY_SKILL = "calendar/search_availability"
EMAIL_DRAFT_SKILL = "email/draft"
EMAIL_SEND_SKILL = "email/send"
IMAGE_GEOSPATIAL_ANALYSIS_SKILL = "image/geospatial_analysis"
INTELLIGENCE_SITUATION_SUMMARY_SKILL = "intelligence/situation_summary"
INTELLIGENCE_COURSE_OF_ACTION_SKILL = "intelligence/course_of_action"
CODE_REPOSITORY_INSPECTION_SKILL = "code/repository_inspection"
CODE_BUG_FIX_SKILL = "code/bug_fix"
CODE_PR_REVIEW_SKILL = "code/pr_review"
CODE_TEST_GENERATION_SKILL = "code/test_generation"
CODE_DOCUMENTATION_SKILL = "code/documentation"


def infer_skill(text: str) -> str:
    return plan_workflow(text)[0]


def plan_workflow(text: str, requested_skill: str | None = None) -> list[str]:
    if requested_skill:
        return [requested_skill]

    lowered = text.lower()
    workflow: list[str] = []

    code_skill = _code_skill(lowered)
    if code_skill:
        workflow.append(code_skill)
    if _mentions_image_geo(lowered):
        workflow.append(IMAGE_GEOSPATIAL_ANALYSIS_SKILL)
    if any(term in lowered for term in WEATHER_TERMS):
        workflow.append(WEATHER_SKILL)
    if _mentions_calendar(lowered):
        workflow.append(CALENDAR_SEARCH_AVAILABILITY_SKILL)
    if _mentions_email(lowered):
        if "draft" in lowered or "meeting option" in lowered:
            workflow.append(EMAIL_DRAFT_SKILL)
        elif "send" in lowered:
            workflow.append(EMAIL_SEND_SKILL)
        else:
            workflow.append(EMAIL_DRAFT_SKILL)
    if _mentions_intelligence(lowered):
        if "course of action" in lowered:
            workflow.append(INTELLIGENCE_COURSE_OF_ACTION_SKILL)
        else:
            workflow.append(INTELLIGENCE_SITUATION_SUMMARY_SKILL)
    if workflow:
        return workflow
    return [WEATHER_SKILL]


def select_agent(
    *,
    context_id: str,
    user_goal: str,
    directory_client: DirectoryClient,
    skill: str | None = None,
) -> RoutingDecision:
    selected_skill = skill or infer_skill(user_goal)
    candidates = directory_client.search_agents(skill=selected_skill)
    if not candidates:
        raise LookupError(f"No agent found for skill {selected_skill}")
    selected = candidates[0]
    return RoutingDecision(
        context_id=context_id,
        user_goal=user_goal,
        selected_agents=[selected],
        rejected_agents=_rejected(candidates, selected),
        reason=f"Selected highest-ranked agent for skill {selected_skill}.",
        confidence=0.9,
        requires_human_confirmation=False,
    )


def _rejected(candidates: list[DiscoveredAgent], selected: DiscoveredAgent) -> list[DiscoveredAgent]:
    return [agent for agent in candidates if agent.agent_id != selected.agent_id]


def _mentions_calendar(lowered: str) -> bool:
    return any(term in lowered for term in CALENDAR_TERMS)


def _mentions_email(lowered: str) -> bool:
    return any(term in lowered for term in EMAIL_TERMS)


def _mentions_image_geo(lowered: str) -> bool:
    return any(term in lowered for term in IMAGE_GEO_TERMS)


def _mentions_intelligence(lowered: str) -> bool:
    return any(term in lowered for term in INTELLIGENCE_TERMS)


def _code_skill(lowered: str) -> str | None:
    if not any(term in lowered for term in CODE_TERMS):
        return None
    if "pull request" in lowered or " pr " in f" {lowered} ":
        return CODE_PR_REVIEW_SKILL
    if "fix" in lowered or "failing test" in lowered or "bug" in lowered:
        return CODE_BUG_FIX_SKILL
    if "generate test" in lowered or "write test" in lowered or "add test" in lowered:
        return CODE_TEST_GENERATION_SKILL
    if "documentation" in lowered or "docs" in lowered:
        return CODE_DOCUMENTATION_SKILL
    return CODE_REPOSITORY_INSPECTION_SKILL
