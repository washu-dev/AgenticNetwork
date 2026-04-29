from __future__ import annotations

from v2_agent_network.services.common.models import DiscoveredAgent, RoutingDecision
from v2_agent_network.services.directory.client import DirectoryClient


WEATHER_TERMS = {"weather", "forecast", "temperature", "rain", "wind", "sunny", "cloudy"}
CALENDAR_TERMS = {"calendar", "schedule", "availability", "available", "meeting options"}
EMAIL_TERMS = {"email", "mail", "message"}
DOC_TERMS = {"google doc", "google docs", "document", "docs"}
SHEET_TERMS = {"google sheet", "google sheets", "spreadsheet", "sheet"}
IMAGE_GEO_TERMS = {"photo", "image", "geospatial", "satellite", "scene"}
INTELLIGENCE_TERMS = {"situation", "summary", "plan", "risk", "course of action"}
CODE_TERMS = {"repo", "repository", "code", "coding", "test", "tests", "bug", "fix", "pr", "pull request"}
WEATHER_SKILL = "weather/forecast"
CALENDAR_SEARCH_AVAILABILITY_SKILL = "calendar/search_availability"
EMAIL_DRAFT_SKILL = "email/draft"
EMAIL_SEND_SKILL = "email/send"
EMAIL_SEARCH_SKILL = "email/search"
EMAIL_READ_SKILL = "email/read"
EMAIL_SUMMARIZE_THREAD_SKILL = "email/summarize_thread"
EMAIL_GOOGLE_STATUS_SKILL = "email/google/status"
EMAIL_CURRENT_TIME_SKILL = "email/current_time"
EMAIL_CALENDAR_LIST_EVENTS_SKILL = "email/calendar/list_events"
EMAIL_CALENDAR_CREATE_EVENT_SKILL = "email/calendar/create_event"
EMAIL_CALENDAR_STATUS_SKILL = "email/calendar/status"
EMAIL_DOCS_LIST_SKILL = "email/docs/list"
EMAIL_DOCS_READ_SKILL = "email/docs/read"
EMAIL_DOCS_CREATE_SKILL = "email/docs/create"
EMAIL_DOCS_APPEND_SKILL = "email/docs/append"
EMAIL_DOCS_REPLACE_TEXT_SKILL = "email/docs/replace_text"
EMAIL_DOCS_CREATE_AND_EMAIL_SKILL = "email/docs/create_and_email"
EMAIL_SHEETS_LIST_SKILL = "email/sheets/list"
EMAIL_SHEETS_READ_SKILL = "email/sheets/read"
EMAIL_SHEETS_CREATE_SKILL = "email/sheets/create"
EMAIL_SHEETS_WRITE_SKILL = "email/sheets/write"
EMAIL_SHEETS_APPEND_SKILL = "email/sheets/append"
IMAGE_GEOSPATIAL_ANALYSIS_SKILL = "image/geospatial_analysis"
INTELLIGENCE_SITUATION_SUMMARY_SKILL = "intelligence/situation_summary"
INTELLIGENCE_COURSE_OF_ACTION_SKILL = "intelligence/course_of_action"
CODE_REPOSITORY_INSPECTION_SKILL = "code/repository_inspection"
CODE_BUG_FIX_SKILL = "code/bug_fix"
CODE_PR_REVIEW_SKILL = "code/pr_review"
CODE_TEST_GENERATION_SKILL = "code/test_generation"
CODE_DOCUMENTATION_SKILL = "code/documentation"

ALL_KNOWN_SKILLS = [
    WEATHER_SKILL,
    CALENDAR_SEARCH_AVAILABILITY_SKILL,
    EMAIL_DRAFT_SKILL,
    EMAIL_SEND_SKILL,
    EMAIL_SEARCH_SKILL,
    EMAIL_READ_SKILL,
    EMAIL_SUMMARIZE_THREAD_SKILL,
    EMAIL_GOOGLE_STATUS_SKILL,
    EMAIL_CURRENT_TIME_SKILL,
    EMAIL_CALENDAR_LIST_EVENTS_SKILL,
    EMAIL_CALENDAR_CREATE_EVENT_SKILL,
    EMAIL_CALENDAR_STATUS_SKILL,
    EMAIL_DOCS_LIST_SKILL,
    EMAIL_DOCS_READ_SKILL,
    EMAIL_DOCS_CREATE_SKILL,
    EMAIL_DOCS_APPEND_SKILL,
    EMAIL_DOCS_REPLACE_TEXT_SKILL,
    EMAIL_DOCS_CREATE_AND_EMAIL_SKILL,
    EMAIL_SHEETS_LIST_SKILL,
    EMAIL_SHEETS_READ_SKILL,
    EMAIL_SHEETS_CREATE_SKILL,
    EMAIL_SHEETS_WRITE_SKILL,
    EMAIL_SHEETS_APPEND_SKILL,
    IMAGE_GEOSPATIAL_ANALYSIS_SKILL,
    INTELLIGENCE_SITUATION_SUMMARY_SKILL,
    INTELLIGENCE_COURSE_OF_ACTION_SKILL,
    CODE_REPOSITORY_INSPECTION_SKILL,
    CODE_BUG_FIX_SKILL,
    CODE_PR_REVIEW_SKILL,
    CODE_TEST_GENERATION_SKILL,
    CODE_DOCUMENTATION_SKILL,
]


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
    utility_skill = _email_utility_skill(lowered)
    if utility_skill:
        workflow.append(utility_skill)
    docs_skill = None
    if not code_skill or any(term in lowered for term in {"google doc", "google docs", "document"}):
        docs_skill = _docs_skill(lowered)
    if docs_skill:
        workflow.append(docs_skill)
    sheets_skill = _sheets_skill(lowered)
    if sheets_skill:
        workflow.append(sheets_skill)
    if _mentions_calendar(lowered) and utility_skill != EMAIL_CALENDAR_STATUS_SKILL:
        workflow.append(_calendar_skill(lowered))
    if _mentions_email(lowered) and docs_skill != EMAIL_DOCS_CREATE_AND_EMAIL_SKILL and not utility_skill:
        workflow.append(_email_skill(lowered))
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


def _calendar_skill(lowered: str) -> str:
    if "status" in lowered or "configured" in lowered or "oauth" in lowered:
        return EMAIL_CALENDAR_STATUS_SKILL
    if any(term in lowered for term in {"list", "show", "what is on", "what's on", "events"}):
        return EMAIL_CALENDAR_LIST_EVENTS_SKILL
    if any(term in lowered for term in {"create", "add", "schedule", "invite", "event"}):
        if "availability" not in lowered and "available" not in lowered and "meeting option" not in lowered:
            return EMAIL_CALENDAR_CREATE_EVENT_SKILL
    return CALENDAR_SEARCH_AVAILABILITY_SKILL


def _docs_skill(lowered: str) -> str | None:
    if not any(term in lowered for term in DOC_TERMS):
        return None
    if any(term in lowered for term in {"email", "mail", "send", "share"}):
        if any(term in lowered for term in {"create", "new", "draft", "write"}):
            return EMAIL_DOCS_CREATE_AND_EMAIL_SKILL
    if any(term in lowered for term in {"create", "new", "draft", "write"}):
        return EMAIL_DOCS_CREATE_SKILL
    if "append" in lowered or "add text" in lowered:
        return EMAIL_DOCS_APPEND_SKILL
    if "replace" in lowered:
        return EMAIL_DOCS_REPLACE_TEXT_SKILL
    if any(term in lowered for term in {"list", "find", "search"}):
        return EMAIL_DOCS_LIST_SKILL
    return EMAIL_DOCS_READ_SKILL


def _sheets_skill(lowered: str) -> str | None:
    if not any(term in lowered for term in SHEET_TERMS):
        return None
    if any(term in lowered for term in {"create", "new"}):
        return EMAIL_SHEETS_CREATE_SKILL
    if "append" in lowered or "add row" in lowered:
        return EMAIL_SHEETS_APPEND_SKILL
    if any(term in lowered for term in {"write", "overwrite", "update"}):
        return EMAIL_SHEETS_WRITE_SKILL
    if any(term in lowered for term in {"list", "find", "search"}):
        return EMAIL_SHEETS_LIST_SKILL
    return EMAIL_SHEETS_READ_SKILL


def _email_utility_skill(lowered: str) -> str | None:
    if "current time" in lowered or ("what time" in lowered and "email" in lowered):
        return EMAIL_CURRENT_TIME_SKILL
    if "calendar" in lowered and ("status" in lowered or "configured" in lowered or "oauth" in lowered):
        return EMAIL_CALENDAR_STATUS_SKILL
    if ("google" in lowered or "workspace" in lowered or "oauth" in lowered) and "status" in lowered:
        return EMAIL_GOOGLE_STATUS_SKILL
    return None


def _email_skill(lowered: str) -> str:
    if "summarize" in lowered or "summary" in lowered or "thread" in lowered:
        return EMAIL_SUMMARIZE_THREAD_SKILL
    if "search" in lowered or "find" in lowered:
        return EMAIL_SEARCH_SKILL
    if "read" in lowered or "latest" in lowered or "unread" in lowered:
        return EMAIL_READ_SKILL
    if "send" in lowered:
        return EMAIL_SEND_SKILL
    return EMAIL_DRAFT_SKILL


def _mentions_image_geo(lowered: str) -> bool:
    return any(term in lowered for term in IMAGE_GEO_TERMS)


def _mentions_intelligence(lowered: str) -> bool:
    return any(term in lowered for term in INTELLIGENCE_TERMS)


def _code_skill(lowered: str) -> str | None:
    if not _mentions_code(lowered):
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


def _mentions_code(lowered: str) -> bool:
    code_terms = CODE_TERMS - {"pr"}
    return any(term in lowered for term in code_terms) or " pr " in f" {lowered} "
