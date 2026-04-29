from __future__ import annotations

import pytest

from v2_agent_network.services.pi_agent.policy import evaluate_skill
from v2_agent_network.services.pi_agent.router import plan_workflow


READ_ONLY_SKILLS = {
    "email/search",
    "email/read",
    "email/draft",
    "email/summarize_thread",
    "email/google/status",
    "email/current_time",
    "email/calendar/status",
    "email/calendar/list_events",
    "email/docs/list",
    "email/docs/read",
    "email/sheets/list",
    "email/sheets/read",
}
WRITE_SKILLS = {
    "email/send",
    "email/calendar/create_event",
    "email/docs/create",
    "email/docs/append",
    "email/docs/replace_text",
    "email/docs/create_and_email",
    "email/sheets/create",
    "email/sheets/write",
    "email/sheets/append",
}


@pytest.mark.parametrize("skill", sorted(READ_ONLY_SKILLS))
def test_live_email_read_only_skills_are_allowed(skill: str) -> None:
    assert evaluate_skill(skill).status == "allowed"


@pytest.mark.parametrize("skill", sorted(WRITE_SKILLS))
def test_live_email_write_skills_require_confirmation(skill: str) -> None:
    decision = evaluate_skill(skill)
    assert decision.status == "requires_confirmation"
    assert decision.confirmation_status == "pending"


def test_email_draft_gmail_draft_creation_requires_confirmation() -> None:
    assert evaluate_skill("email/draft", metadata={"create_gmail_draft": False}).status == "allowed"
    assert evaluate_skill("email/draft", metadata={"create_gmail_draft": "false"}).status == "allowed"

    decision = evaluate_skill("email/draft", metadata={"create_gmail_draft": True})
    assert decision.status == "requires_confirmation"
    assert decision.confirmation_status == "pending"
    assert "create_gmail_draft=true" in decision.reason


def test_permissive_policy_mode_allows_write_skills_without_confirmation(monkeypatch) -> None:
    monkeypatch.setenv("PI_POLICY_MODE", "permissive")

    decision = evaluate_skill("email/send")

    assert decision.status == "allowed"
    assert decision.confirmation_status == "not_required"
    assert "PI_POLICY_MODE=permissive" in decision.reason


def test_router_maps_common_merged_email_calendar_workspace_prompts() -> None:
    assert plan_workflow("What time is it for the email agent?") == ["email/current_time"]
    assert plan_workflow("Check Google Workspace OAuth status.") == ["email/google/status"]
    assert plan_workflow("Check whether Calendar access is configured.") == [
        "email/calendar/status"
    ]
    assert plan_workflow("List my calendar events tomorrow.") == ["email/calendar/list_events"]
    assert plan_workflow("Create a calendar event for Friday.") == [
        "email/calendar/create_event"
    ]
    assert plan_workflow("Find my Google Docs about the project.") == ["email/docs/list"]
    assert plan_workflow("Read this Google Doc by ID.") == ["email/docs/read"]
    assert plan_workflow("Append these notes to the project Google Doc.") == [
        "email/docs/append"
    ]
    assert plan_workflow("Replace TODO in this Google Doc.") == ["email/docs/replace_text"]
    assert plan_workflow("Find my Google Sheets about expenses.") == ["email/sheets/list"]
    assert plan_workflow("Read Sheet1 from this spreadsheet.") == ["email/sheets/read"]
    assert plan_workflow("Append this row to the tracker spreadsheet.") == [
        "email/sheets/append"
    ]
    assert plan_workflow("Write these rows to this Google Sheet.") == ["email/sheets/write"]
