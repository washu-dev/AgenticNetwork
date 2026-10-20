from __future__ import annotations

import os
from collections.abc import Mapping
from pydantic import BaseModel, Field

from chat_app.env import setting


class PlannerConfigError(RuntimeError):
    """Raised when the planner configuration is invalid."""


class PlannerConfig(BaseModel):
    """Runtime config for the planner.

    The planner is LLM-driven by default. When llm_enabled is False,
    a deterministic mock client (used for tests) must be passed in.
    """

    llm_enabled: bool = True
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    max_output_tokens: int = Field(1500, gt=0)
    timeout_seconds: float = Field(60, gt=0)
    max_steps: int = Field(10, gt=0)

    # Optional automatic failover: when the primary provider is unavailable
    # (connection error / timeout / 5xx), retry the same request against this
    # fallback provider. Left unset → no failover (single-provider behaviour).
    llm_fallback_provider: str | None = None
    llm_fallback_base_url: str | None = None
    llm_fallback_model: str | None = None
    # Shorter timeout for the primary attempt when a fallback is configured, so
    # a down primary fails over quickly instead of hanging the whole request.
    llm_primary_timeout_seconds: float = Field(20, gt=0)

    fake_ads: bool = False
    ads_server_addr: str | None = None

    policy_mode: str = "permissive"
    history_limit: int = Field(20, gt=0)

    # Legacy agents (by agent_id) that still get the user's whole credential bundle
    # instead of only the types they declared and were granted (runtime._scope_credentials).
    unscoped_credential_agents: list[str] = Field(default_factory=list)


def load_config(env: Mapping[str, str] | None = None) -> PlannerConfig:
    values = env if env is not None else os.environ
    config = PlannerConfig(
        llm_enabled=_bool(setting("PLANNER_LLM_ENABLED", env=values), default=True),
        llm_provider=setting("PLANNER_LLM_PROVIDER", "openai", env=values).strip().lower(),
        llm_model=setting("PLANNER_LLM_MODEL", "gpt-4o-mini", env=values).strip(),
        max_output_tokens=_int(setting("PLANNER_LLM_MAX_OUTPUT_TOKENS", env=values), default=1500),
        timeout_seconds=_float(setting("PLANNER_LLM_TIMEOUT_SECONDS", env=values), default=60),
        max_steps=_int(setting("PLANNER_MAX_STEPS", env=values), default=10),
        fake_ads=_bool(values.get("AGENT_NETWORK_FAKE_ADS"), default=False),
        ads_server_addr=_optional(values.get("ADS_SERVER_ADDR") or values.get("ADS_URL")),
        policy_mode=setting("PLANNER_POLICY_MODE", "permissive", env=values).strip().lower(),
        history_limit=_int(setting("PLANNER_HISTORY_LIMIT", env=values), default=20),
        llm_fallback_provider=_lower_optional(setting("PLANNER_LLM_FALLBACK_PROVIDER", env=values)),
        llm_fallback_base_url=_optional(setting("PLANNER_LLM_FALLBACK_BASE_URL", env=values)),
        llm_fallback_model=_optional(setting("PLANNER_LLM_FALLBACK_MODEL", env=values)),
        llm_primary_timeout_seconds=_float(
            setting("PLANNER_LLM_PRIMARY_TIMEOUT_SECONDS", env=values), default=20
        ),
        unscoped_credential_agents=[
            a.strip() for a in values.get("PLANNER_UNSCOPED_CREDENTIAL_AGENTS", "").split(",") if a.strip()
        ],
    )
    if config.llm_enabled and config.llm_provider == "openai" and not values.get("OPENAI_API_KEY"):
        raise PlannerConfigError(
            "PLANNER_LLM_ENABLED=true with provider=openai requires OPENAI_API_KEY."
        )
    if config.llm_fallback_provider and not config.llm_fallback_model:
        raise PlannerConfigError(
            "PLANNER_LLM_FALLBACK_PROVIDER is set but PLANNER_LLM_FALLBACK_MODEL is missing; "
            "the fallback provider serves a different model name than the primary."
        )
    return config


def _bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str | None, *, default: int) -> int:
    return int(value) if value else default


def _float(value: str | None, *, default: float) -> float:
    return float(value) if value else default


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _lower_optional(value: str | None) -> str | None:
    stripped = _optional(value)
    return stripped.lower() if stripped else None
