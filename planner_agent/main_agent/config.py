from __future__ import annotations

import os
from collections.abc import Mapping
from pydantic import BaseModel, Field


class MainAgentConfigError(RuntimeError):
    """Raised when MainAgent configuration is invalid."""


class MainAgentConfig(BaseModel):
    """Runtime config for the planner agent.

    The main agent is LLM-driven by default. When llm_enabled is False,
    a deterministic mock client (used for tests) must be passed in.
    """

    llm_enabled: bool = True
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    reasoning_effort: str | None = None
    verbosity: str | None = None
    max_output_tokens: int = Field(1500, gt=0)
    timeout_seconds: float = Field(60, gt=0)
    max_steps: int = Field(10, gt=0)

    fake_ads: bool = False
    ads_server_addr: str | None = None
    ads_backend: str = "cli"

    policy_mode: str = "permissive"
    history_limit: int = Field(20, gt=0)


def load_config(env: Mapping[str, str] | None = None) -> MainAgentConfig:
    values = env if env is not None else os.environ
    config = MainAgentConfig(
        llm_enabled=_bool(values.get("MAIN_AGENT_LLM_ENABLED"), default=True),
        llm_provider=values.get("MAIN_AGENT_LLM_PROVIDER", "openai").strip().lower(),
        llm_model=values.get("MAIN_AGENT_LLM_MODEL", "gpt-4o-mini").strip(),
        reasoning_effort=_optional(values.get("MAIN_AGENT_REASONING_EFFORT")),
        verbosity=_optional(values.get("MAIN_AGENT_VERBOSITY")),
        max_output_tokens=_int(values.get("MAIN_AGENT_MAX_OUTPUT_TOKENS"), default=1500),
        timeout_seconds=_float(values.get("MAIN_AGENT_TIMEOUT_SECONDS"), default=60),
        max_steps=_int(values.get("MAIN_AGENT_MAX_STEPS"), default=10),
        fake_ads=_bool(values.get("AGENT_NETWORK_FAKE_ADS"), default=False),
        ads_server_addr=_optional(values.get("ADS_SERVER_ADDR") or values.get("ADS_URL")),
        ads_backend=values.get("ADS_BACKEND", "cli").strip().lower(),
        policy_mode=values.get("MAIN_AGENT_POLICY_MODE", "permissive").strip().lower(),
        history_limit=_int(values.get("MAIN_AGENT_HISTORY_LIMIT"), default=20),
    )
    if config.llm_enabled and config.llm_provider == "openai" and not values.get("OPENAI_API_KEY"):
        raise MainAgentConfigError(
            "MAIN_AGENT_LLM_ENABLED=true with provider=openai requires OPENAI_API_KEY."
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
