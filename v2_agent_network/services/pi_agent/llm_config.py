from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, Field


class PILLMConfigError(RuntimeError):
    """Raised when PI LLM planner configuration is invalid."""


class PILLMConfig(BaseModel):
    enabled: bool = False
    provider: str = "openai"
    model: str = "gpt-5.5"
    reasoning_effort: str | None = "low"
    verbosity: str | None = "low"
    max_output_tokens: int = Field(1200, gt=0)
    timeout_seconds: float = Field(30, gt=0)
    allow_deterministic_fallback: bool = False
    use_for_final_summary: bool = True


def load_pi_llm_config(env: Mapping[str, str] | None = None) -> PILLMConfig:
    values = env if env is not None else os.environ
    config = PILLMConfig(
        enabled=_bool(values.get("PI_LLM_ENABLED"), default=False),
        provider=values.get("PI_LLM_PROVIDER", "openai").strip().lower(),
        model=values.get("PI_LLM_MODEL", "gpt-5.5").strip(),
        reasoning_effort=_optional(values.get("PI_LLM_REASONING_EFFORT", "low")),
        verbosity=_optional(values.get("PI_LLM_VERBOSITY", "low")),
        max_output_tokens=_int(values.get("PI_LLM_MAX_OUTPUT_TOKENS"), default=1200),
        timeout_seconds=_float(values.get("PI_LLM_TIMEOUT_SECONDS"), default=30),
        allow_deterministic_fallback=_bool(
            values.get("PI_LLM_ALLOW_DETERMINISTIC_FALLBACK"),
            default=False,
        ),
        use_for_final_summary=_bool(values.get("PI_LLM_USE_FOR_FINAL_SUMMARY"), default=True),
    )
    if not config.enabled:
        return config
    if config.provider != "openai":
        raise PILLMConfigError(
            f"Unsupported PI_LLM_PROVIDER={config.provider!r}; only 'openai' is supported."
        )
    if not values.get("OPENAI_API_KEY"):
        raise PILLMConfigError(
            "PI_LLM_ENABLED=true requires OPENAI_API_KEY in the PI server environment."
        )
    return config


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str | None, *, default: int) -> int:
    return int(value) if value else default


def _float(value: str | None, *, default: float) -> float:
    return float(value) if value else default
