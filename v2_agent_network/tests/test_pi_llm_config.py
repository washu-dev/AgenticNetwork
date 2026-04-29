from __future__ import annotations

import pytest

from v2_agent_network.services.pi_agent.llm_config import (
    PILLMConfigError,
    load_pi_llm_config,
)
from v2_agent_network.services.pi_agent.router import plan_workflow


def test_pi_llm_disabled_keeps_deterministic_routing() -> None:
    config = load_pi_llm_config({"PI_LLM_ENABLED": "false"})

    assert config.enabled is False
    assert plan_workflow("What time is it for the email agent?") == ["email/current_time"]


def test_pi_llm_enabled_requires_openai_api_key() -> None:
    with pytest.raises(PILLMConfigError) as exc_info:
        load_pi_llm_config({"PI_LLM_ENABLED": "true", "PI_LLM_PROVIDER": "openai"})

    assert "OPENAI_API_KEY" in str(exc_info.value)


def test_pi_llm_config_loads_openai_values_without_exposing_key() -> None:
    config = load_pi_llm_config(
        {
            "PI_LLM_ENABLED": "true",
            "PI_LLM_PROVIDER": "openai",
            "PI_LLM_MODEL": "gpt-5.5",
            "PI_LLM_REASONING_EFFORT": "low",
            "PI_LLM_VERBOSITY": "low",
            "PI_LLM_MAX_OUTPUT_TOKENS": "900",
            "PI_LLM_TIMEOUT_SECONDS": "12.5",
            "PI_LLM_ALLOW_DETERMINISTIC_FALLBACK": "true",
            "PI_LLM_USE_FOR_FINAL_SUMMARY": "false",
            "OPENAI_API_KEY": "sk-test-not-printed",
        }
    )

    assert config.enabled is True
    assert config.model == "gpt-5.5"
    assert config.reasoning_effort == "low"
    assert config.verbosity == "low"
    assert config.max_output_tokens == 900
    assert config.timeout_seconds == 12.5
    assert config.allow_deterministic_fallback is True
    assert config.use_for_final_summary is False
    assert "sk-test" not in repr(config)
