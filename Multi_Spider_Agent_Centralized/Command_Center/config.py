"""
Command Center configuration.

All config in one place: LLM, server, transport, detection, health check.
Values come from environment variables (set in .env file).
"""

import os
import logging

logger = logging.getLogger(__name__)

# === Server ===
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8080"))

# === LLM (Planner Agent) ===
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))

# === LLM (Robot Agent) ===
PI_BRAIN_LLM_MODEL = os.getenv("PI_BRAIN_LLM_MODEL", LLM_MODEL)

# === LLM API Keys ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", None)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", None)
AZURE_API_KEY = os.getenv("AZURE_API_KEY", None)
AZURE_API_BASE = os.getenv("AZURE_API_BASE", None)
AZURE_API_VERSION = os.getenv("AZURE_API_VERSION", "2024-02-15-preview")
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", None)

# LiteLLM Proxy (optional)
LITELLM_PROXY_BASE_URL = os.getenv("LITELLM_PROXY_BASE_URL", None)
LITELLM_PROXY_API_KEY = os.getenv("LITELLM_PROXY_API_KEY", None)

# === Robot discovery ===
DISCOVERY_MODE = os.getenv("DISCOVERY_MODE", "static")  # "static" or "ads"
ADS_SERVER_ADDRESS = os.getenv("ADS_SERVER_ADDRESS", "localhost:8888")

# Static agent list (used when DISCOVERY_MODE="static")
_raw_agents = os.getenv("PI_AGENTS", "")
if _raw_agents:
    import json as _json
    PI_AGENTS = _json.loads(_raw_agents)
else:
    PI_AGENTS = [
        {"id": "crawler-alpha-patrol-001", "host": "localhost", "port": 9004},
    ]

# === Robot defaults ===
DEFAULT_CAMERA_PORT = int(os.getenv("DEFAULT_CAMERA_PORT", "9000"))

# === NATS Transport (Planner ↔ Robot Agent) ===
DEFAULT_MESSAGE_TRANSPORT = os.getenv("DEFAULT_MESSAGE_TRANSPORT", "NATS")
TRANSPORT_SERVER_ENDPOINT = os.getenv("TRANSPORT_SERVER_ENDPOINT", "nats://localhost:4222")

# === Detection engine ===
MISSION_TIMEOUT_STATIONARY = int(os.getenv("MISSION_TIMEOUT_STATIONARY", "10"))
MISSION_TIMEOUT_PATROL = int(os.getenv("MISSION_TIMEOUT_PATROL", "180"))

# === Health check ===
HEALTH_CHECK_TIMEOUT = int(os.getenv("HEALTH_CHECK_TIMEOUT", "5"))
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "15"))


# === LLM Factory Functions ===

_cached_llm = None


def create_llm():
    """Create or return cached LangChain LLM for Planner Agent."""
    global _cached_llm
    if _cached_llm is not None:
        return _cached_llm

    if "/" in LLM_MODEL:
        provider, model_name = LLM_MODEL.split("/", 1)
    else:
        provider, model_name = "openai", LLM_MODEL

    provider = provider.lower()
    logger.info(f"Initializing Planner LLM: {LLM_MODEL}")

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        _cached_llm = ChatAnthropic(model=model_name, temperature=OPENAI_TEMPERATURE, max_tokens=LLM_MAX_TOKENS)
    elif provider == "azure":
        from langchain_openai import AzureChatOpenAI
        _cached_llm = AzureChatOpenAI(deployment_name=model_name, api_version=AZURE_API_VERSION,
                                       temperature=OPENAI_TEMPERATURE, max_tokens=LLM_MAX_TOKENS)
    else:
        from langchain_openai import ChatOpenAI
        _cached_llm = ChatOpenAI(model=model_name, temperature=OPENAI_TEMPERATURE, max_tokens=LLM_MAX_TOKENS)

    return _cached_llm


def create_vision_llm():
    """Create a vision-capable LLM for image analysis."""
    vision_model = os.getenv("VISION_MODEL", "gpt-4o")
    logger.info(f"Initializing Vision LLM: {vision_model}")
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=vision_model, temperature=0.3, max_tokens=LLM_MAX_TOKENS)


def validate_llm_config() -> bool:
    """Validate LLM configuration."""
    if not LLM_MODEL:
        logger.error("LLM_MODEL is not configured")
        return False
    provider = LLM_MODEL.split("/")[0].lower()
    if provider == "openai" and not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY is not set")
        return False
    elif provider == "anthropic" and not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY is not set")
        return False
    logger.info(f"LLM configuration validated: {LLM_MODEL}")
    return True
