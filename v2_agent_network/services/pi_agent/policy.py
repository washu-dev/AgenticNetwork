from __future__ import annotations

from v2_agent_network.services.common.models import (
    AgentCapability,
    ConfirmationStatus,
    PolicyDecision,
    PolicyDecisionStatus,
    RiskLevel,
)


CONFIRMATION_REQUIRED_SKILLS = {
    "email/send",
    "calendar/create_event",
    "calendar/update_event",
    "calendar/delete_event",
    "code/bug_fix",
    "code/test_generation",
}

ALLOWED_SKILLS = {
    "weather/forecast",
    "calendar/search_availability",
    "calendar/respond_invitation",
    "calendar/summarize_schedule",
    "image/geospatial_analysis",
    "image/object_detection",
    "image/location_inference",
    "image/satellite_interpretation",
    "image/photo_forensics_basic",
    "email/search",
    "email/read",
    "email/draft",
    "email/summarize_thread",
    "intelligence/situation_summary",
    "intelligence/risk_assessment",
    "intelligence/course_of_action",
    "intelligence/entity_tracking",
    "intelligence/report_generation",
    "code/repository_inspection",
    "code/pr_review",
    "code/documentation",
}


def evaluate_skill(skill: str, capability: AgentCapability | None = None) -> PolicyDecision:
    if skill in CONFIRMATION_REQUIRED_SKILLS:
        return PolicyDecision(
            status=PolicyDecisionStatus.requires_confirmation,
            reason=f"{skill} may change external or repository state and requires confirmation.",
            confirmation_status=ConfirmationStatus.pending,
        )
    if skill in ALLOWED_SKILLS:
        return PolicyDecision(
            status=PolicyDecisionStatus.allowed,
            reason=f"{skill} is allowed in mock Milestone 3A policy.",
            confirmation_status=ConfirmationStatus.not_required,
        )
    if capability is not None:
        return evaluate_capability(capability)
    return PolicyDecision(
        status=PolicyDecisionStatus.denied,
        reason=f"No policy rule exists for {skill}.",
        confirmation_status=ConfirmationStatus.not_required,
    )


def evaluate_capability(capability: AgentCapability, skill: str | None = None) -> PolicyDecision:
    if skill is not None:
        return evaluate_skill(skill, capability)
    if capability.action_type == "write" or capability.risk_level in {RiskLevel.medium, RiskLevel.high}:
        return PolicyDecision(
            status=PolicyDecisionStatus.requires_confirmation,
            reason="Write or elevated-risk action requires confirmation.",
            confirmation_status=ConfirmationStatus.pending,
        )
    return PolicyDecision(
        status=PolicyDecisionStatus.allowed,
        reason="Read-only low-risk mock Weather request.",
        confirmation_status=ConfirmationStatus.not_required,
    )
