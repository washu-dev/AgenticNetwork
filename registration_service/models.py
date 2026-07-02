"""Pydantic models for the registration service API."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from registration_service.credential_catalog import is_known_type


class OASFSkill(BaseModel):
    name: str
    id: int | None = None
    key: str | None = None   # canonical path-style id used for the card skill.id + ADS routing


class CredentialRequirement(BaseModel):
    """A user-owned credential a custom agent consumes (P4a).

    ``type`` is a canonical catalog key (e.g. ``openai_api_key``) or a
    ``custom:<slug>`` escape — see ``registration_service/credential_catalog.py``.
    ``required`` drives P4c enforcement (required→block if missing; optional→proceed);
    ``env_var`` names the variable the agent reads the value under in its context.
    """
    type: str
    label: str
    required: bool = True
    env_var: str | None = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        v = v.strip()
        if not is_known_type(v):
            raise ValueError(
                f"Unknown credential type: {v!r} "
                "(use a catalog type or 'custom:<slug>')"
            )
        return v


class LLMConfig(BaseModel):
    system_prompt: str
    model: str = "openai/gpt-oss-120b"
    tools: list[dict[str, Any]] = Field(default_factory=list)


class APIProxyConfig(BaseModel):
    upstream_url: str
    auth_header: str | None = None


HARD_MAX_RUNTIME_SECONDS = 3600   # ceiling for a single agent request (D11)

# apt package tokens permitted in system_packages. Debian names are lowercase
# letters/digits plus +.-, but we also allow version pins (pkg=1.2) and arch
# qualifiers (pkg:amd64). This is NOT a judgment about which packages are
# "safe" (D10 permits arbitrary apt, flagged for the approver) — it purely
# blocks shell/Dockerfile injection by rejecting whitespace and metacharacters
# before the token reaches the generated `apt-get install` RUN line.
_APT_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+.:=~-]*$")


class CustomConfig(BaseModel):
    class_name: str
    python_version: str = "3.11"
    requirements: list[str] = Field(default_factory=list)
    system_packages: list[str] = Field(default_factory=list)   # apt, cluster build only (D10)
    entry_file: str = "handler.py"          # adapter module at the upload root that defines class_name
    install_project: bool = True            # pip install . if pyproject.toml/setup.py present at root
    max_runtime_seconds: int = 1800         # per-request cap; clamped to HARD_MAX_RUNTIME_SECONDS
    credentials: list[CredentialRequirement] = Field(default_factory=list)   # P4a: user-owned creds this agent consumes
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("max_runtime_seconds")
    @classmethod
    def _clamp_runtime(cls, v: int) -> int:
        # Clamp to a sane window: non-positive falls back to the default; the
        # hard ceiling protects the node from a runaway request.
        if v <= 0:
            return 1800
        return min(v, HARD_MAX_RUNTIME_SECONDS)

    @field_validator("system_packages")
    @classmethod
    def _sanitize_apt(cls, v: list[str]) -> list[str]:
        # Reject tokens that could break out of the `apt-get install` RUN line
        # (injection guard, not a package allowlist — see _APT_TOKEN_RE).
        cleaned: list[str] = []
        for pkg in v:
            pkg = pkg.strip()
            if not pkg:
                continue
            if not _APT_TOKEN_RE.match(pkg):
                raise ValueError(f"Invalid apt package name: {pkg!r}")
            cleaned.append(pkg)
        return cleaned


def parse_config(handler_type: str, raw: dict) -> LLMConfig | APIProxyConfig | CustomConfig:
    """Build the typed config model for a handler_type from a raw dict."""
    if handler_type == "llm":
        return LLMConfig(**raw)
    if handler_type == "api_proxy":
        return APIProxyConfig(**raw)
    if handler_type == "custom":
        return CustomConfig(**raw)
    raise ValueError(f"Unknown handler_type: {handler_type}")


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1)
    handler_type: Literal["llm", "api_proxy", "custom"]
    oasf_skills: list[OASFSkill] = Field(..., min_length=1)
    config: LLMConfig | APIProxyConfig | CustomConfig

    @field_validator("config", mode="before")
    @classmethod
    def validate_config(cls, v: Any, info: Any) -> Any:
        # Pydantic will attempt each model; let it discriminate by required fields.
        # If already a model instance, pass through.
        if isinstance(v, (LLMConfig, APIProxyConfig, CustomConfig)):
            return v
        return v

    def typed_config(self) -> LLMConfig | APIProxyConfig | CustomConfig:
        raw = self.config if isinstance(self.config, dict) else self.config.model_dump()
        return parse_config(self.handler_type, raw)


class RejectRequest(BaseModel):
    reason: str = ""


class SpawnedReport(BaseModel):
    agent_id: uuid.UUID
    pid: int


class AgentResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    handler_type: str
    oasf_skills: list[OASFSkill]
    config: dict[str, Any]
    port: int | None
    status: str
    approved: bool = True
    has_handler: bool = False
    is_archive: bool = False
    endpoint_url: str | None
    ads_cid: str | None
    created_at: datetime
    updated_at: datetime
