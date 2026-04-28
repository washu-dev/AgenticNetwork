from __future__ import annotations

from v2_agent_network.services.common.models import (
    AgentCapability,
    ConfirmationStatus,
    PolicyDecision,
    PolicyDecisionStatus,
    RiskLevel,
)


def evaluate_capability(capability: AgentCapability) -> PolicyDecision:
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

