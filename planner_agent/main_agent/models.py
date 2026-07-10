from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Channel(str, Enum):
    telegram = "telegram"
    whatsapp = "whatsapp"
    api = "api"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class A2ATaskState(str, Enum):
    submitted = "submitted"
    working = "working"
    input_required = "input_required"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"
    auth_required = "auth_required"
    rejected = "rejected"
    unknown = "unknown"


class PolicyDecisionStatus(str, Enum):
    allowed = "allowed"
    requires_confirmation = "requires_confirmation"
    denied = "denied"


class ConfirmationStatus(str, Enum):
    not_required = "not_required"
    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"


class Attachment(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal["file", "image", "audio", "video", "data"] = "file"
    content_type: str = "application/octet-stream"
    uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class UserMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    channel: Channel
    sender_id: str
    sender_display_name: str | None = None
    text: str
    attachments: list[Attachment] = Field(default_factory=list)
    received_at: datetime = Field(default_factory=utc_now)


class PIContext(BaseModel):
    context_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    channel: Channel
    original_message_id: str
    current_goal: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    conversation_history: list[str] = Field(default_factory=list)
    active_tasks: list[str] = Field(default_factory=list)


class OfficialOASFSkill(BaseModel):
    name: str
    id: int | None = None
    source: str = "oasf"


class AgentCapability(BaseModel):
    name: str
    description: str
    skill_tags: list[str] = Field(default_factory=list)
    domain_tags: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=list)
    output_modes: list[str] = Field(default_factory=list)
    required_auth: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.low
    examples: list[str] = Field(default_factory=list)
    official_oasf_skills: list[OfficialOASFSkill] = Field(default_factory=list)
    custom_skill_tags: list[str] = Field(default_factory=list)
    action_type: Literal["read", "write"] = "read"


class CredentialRequirement(BaseModel):
    """A user-owned credential an agent declares it needs (P4b, read-only mirror).

    Parsed from the published card's ``capabilities.extensions`` carrier
    (``agenticnetwork.dev/ext/required-credentials``). Mirrors the registration
    service's model; the planner is a separate deploy unit so it can't import it.
    ``required`` will drive enforcement in P4c; here it is surfaced read-only.
    """
    type: str
    label: str
    required: bool = True
    env_var: str | None = None


class DiscoveredAgent(BaseModel):
    agent_id: str
    name: str
    organization: str
    description: str
    skills: list[AgentCapability] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=list)
    output_modes: list[str] = Field(default_factory=list)
    endpoint: str
    transport: Literal["http", "slim", "nats"] = "http"
    auth: dict[str, Any] = Field(default_factory=dict)
    required_scopes: list[str] = Field(default_factory=list)
    trust_level: float = Field(0.5, ge=0, le=1)
    source_record_id: str
    source_card_url: str | None = None
    raw_record: dict[str, Any] | None = None
    risk_level: RiskLevel = RiskLevel.low
    required_credentials: list[CredentialRequirement] = Field(default_factory=list)


class A2APart(BaseModel):
    kind: Literal["text", "data", "file", "image", "audio", "video"]
    content_type: str = "text/plain"
    text: str | None = None
    data: dict[str, Any] | None = None
    uri: str | None = None
    bytes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_content(self) -> "A2APart":
        if self.kind == "text" and not self.text:
            raise ValueError("text parts require text")
        if self.kind == "data" and self.data is None:
            raise ValueError("data parts require data")
        if self.kind in {"file", "image", "audio", "video"} and not (self.uri or self.bytes):
            raise ValueError(f"{self.kind} parts require uri or bytes")
        return self


class A2AMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    role: Literal["user", "agent"]
    parts: list[A2APart]
    created_at: datetime = Field(default_factory=utc_now)


class A2AArtifact(BaseModel):
    artifact_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    content_type: str = "application/json"
    parts: list[A2APart]
    uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class A2ATaskRequest(BaseModel):
    task_id: str | None = None
    context_id: str
    target_agent_id: str
    skill: str | None = None
    user_goal: str
    messages: list[A2AMessage]
    metadata: dict[str, Any] = Field(default_factory=dict)


class A2ATaskResult(BaseModel):
    task_id: str
    context_id: str
    state: A2ATaskState
    messages: list[A2AMessage] = Field(default_factory=list)
    artifacts: list[A2AArtifact] = Field(default_factory=list)
    structured_output: dict[str, Any] | None = None
    error: str | None = None


class PolicyDecision(BaseModel):
    status: PolicyDecisionStatus
    reason: str
    confirmation_status: ConfirmationStatus = ConfirmationStatus.not_required


class PublishedRecord(BaseModel):
    record_id: str
    agent_id: str
    backend: Literal["fake", "real"]
    raw_record: dict[str, Any]
    backend_ref: str | None = None
