"""Pydantic models for the registration service API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class OASFSkill(BaseModel):
    name: str
    id: int | None = None
    key: str | None = None   # canonical path-style id used for the card skill.id + ADS routing


class LLMConfig(BaseModel):
    system_prompt: str
    model: str = "openai/gpt-oss-120b"
    tools: list[dict[str, Any]] = Field(default_factory=list)


class APIProxyConfig(BaseModel):
    upstream_url: str
    auth_header: str | None = None


class CustomConfig(BaseModel):
    class_name: str
    python_version: str = "3.11"
    requirements: list[str] = Field(default_factory=list)
    entry_file: str = "handler.py"          # adapter module at the upload root that defines class_name
    install_project: bool = True            # pip install . if pyproject.toml/setup.py present at root
    extra: dict[str, Any] = Field(default_factory=dict)


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
