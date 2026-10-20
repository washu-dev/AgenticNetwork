from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field

# The wire models live in agentnet.models and are re-exported here as the same
# objects (not copies): pydantic rejects an instance of a look-alike class, and
# stored pending confirmations are re-validated as DiscoveredAgent.
from agentnet.models import (  # noqa: F401
    A2AArtifact,
    A2AMessage,
    A2APart,
    A2ATaskRequest,
    A2ATaskResult,
    A2ATaskState,
    AgentCapability,
    CredentialRequirement,
    DiscoveredAgent,
    OfficialOASFSkill,
    PublishedRecord,
    RiskLevel,
    utc_now,
)


class Channel(str, Enum):
    api = "api"


class PolicyDecisionStatus(str, Enum):
    allowed = "allowed"
    requires_confirmation = "requires_confirmation"
    denied = "denied"


class ConfirmationStatus(str, Enum):
    not_required = "not_required"
    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"


class PIContext(BaseModel):
    context_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    channel: Channel
    current_goal: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    conversation_history: list[str] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    status: PolicyDecisionStatus
    reason: str
    confirmation_status: ConfirmationStatus = ConfirmationStatus.not_required
