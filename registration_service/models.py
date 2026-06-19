"""Pydantic models for the registration service API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class OASFSkill(BaseModel):
    name: str
    id: int | None = None


class LLMConfig(BaseModel):
    system_prompt: str
    model: str = "openai/gpt-oss-120b"
    tools: list[dict[str, Any]] = Field(default_factory=list)


class APIProxyConfig(BaseModel):
    upstream_url: str
    auth_header: str | None = None


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1)
    handler_type: Literal["llm", "api_proxy"]
    oasf_skills: list[OASFSkill] = Field(..., min_length=1)
    config: LLMConfig | APIProxyConfig

    @field_validator("config", mode="before")
    @classmethod
    def validate_config(cls, v: Any, info: Any) -> Any:
        # Pydantic will attempt both models; let it discriminate by handler_type
        # if already a model instance, pass through
        if isinstance(v, (LLMConfig, APIProxyConfig)):
            return v
        return v

    def typed_config(self) -> LLMConfig | APIProxyConfig:
        if self.handler_type == "llm":
            return LLMConfig.model_validate(
                self.config if isinstance(self.config, dict) else self.config.model_dump()
            )
        return APIProxyConfig.model_validate(
            self.config if isinstance(self.config, dict) else self.config.model_dump()
        )


class AgentResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    handler_type: str
    oasf_skills: list[OASFSkill]
    config: dict[str, Any]
    port: int | None
    status: str
    endpoint_url: str | None
    ads_cid: str | None
    created_at: datetime
    updated_at: datetime
