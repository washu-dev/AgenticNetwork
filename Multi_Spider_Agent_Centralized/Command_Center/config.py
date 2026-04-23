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

# === A2A Transport selection ================================================
# ``DEFAULT_MESSAGE_TRANSPORT`` picks which transport is the AgentCard's
# ``preferred_transport`` — i.e. the default transport A2A clients will
# use when talking to each Robot Agent.  All enabled transports are
# served simultaneously and appear in ``card.additional_interfaces``, so
# clients with other capabilities can negotiate a different one.
#
# Accepted values: NATS | SLIM | HTTP
DEFAULT_MESSAGE_TRANSPORT = os.getenv("DEFAULT_MESSAGE_TRANSPORT", "NATS").upper()


def _envbool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


# By default we always expose HTTP (useful for local debugging / ADS card
# resolution) plus whichever of NATS/SLIM is the preferred transport.
A2A_ENABLE_HTTP = _envbool("A2A_ENABLE_HTTP", True)
A2A_ENABLE_NATS = _envbool("A2A_ENABLE_NATS", DEFAULT_MESSAGE_TRANSPORT == "NATS")
A2A_ENABLE_SLIM = _envbool("A2A_ENABLE_SLIM", DEFAULT_MESSAGE_TRANSPORT == "SLIM")

# Transport endpoints.  ``NATS_ENDPOINT`` / ``SLIM_ENDPOINT`` mirror the
# env-var names used by ``agntcy_app_sdk``'s ``CardBuilder``.
NATS_ENDPOINT = os.getenv(
    "NATS_ENDPOINT",
    os.getenv("TRANSPORT_SERVER_ENDPOINT", "nats://localhost:4222"),
)
SLIM_ENDPOINT = os.getenv("SLIM_ENDPOINT", "http://localhost:46357")
SLIM_SHARED_SECRET = os.getenv(
    "SLIM_SHARED_SECRET",
    "multi-spider-shared-secret-REPLACE-WITH-RANDOM-32PLUS-CHARS",
)

# Backward-compat alias used by the reasoning/state NATS pub/sub path in
# gateway.py / robot_agent.py.  Same broker as A2A's NATS transport.
TRANSPORT_SERVER_ENDPOINT = NATS_ENDPOINT

# === A2A HTTP endpoint (Planner ↔ Robot Agent) ==============================
# Each Robot Agent runs its own a2a-sdk A2AStarletteApplication on HTTP.
# The port is picked deterministically per robot_id from this base +
# modulo of the range (see robot_a2a.ports).
A2A_HTTP_PORT_BASE = int(os.getenv("A2A_HTTP_PORT_BASE", "9100"))
A2A_HTTP_PORT_RANGE = int(os.getenv("A2A_HTTP_PORT_RANGE", "100"))
# Host used by the planner to call Robot Agents when no ADS-provided URL
# is available (i.e. static discovery mode).  In single-machine setups
# this defaults to "localhost".
PLANNER_A2A_HOST_DEFAULT = os.getenv("PLANNER_A2A_HOST_DEFAULT", "localhost")

# === Detection engine ===
MISSION_TIMEOUT_STATIONARY = int(os.getenv("MISSION_TIMEOUT_STATIONARY", "10"))
MISSION_TIMEOUT_PATROL = int(os.getenv("MISSION_TIMEOUT_PATROL", "180"))
# === Approach thresholds ===
# Stage 1: Patrol → Approach (when to stop patrol and start approaching target)
APPROACH_DIST_CM = int(os.getenv("APPROACH_DIST_CM", "50"))            # ultrasonic threshold (cm)
APPROACH_BBOX_RATIO = float(os.getenv("APPROACH_BBOX_RATIO", "0.30"))  # bbox height ratio (0.30 = 30% of frame)

# Stage 2: Approach → Execute action (when to stop approaching and perform final action)
ACTION_DIST_CM = int(os.getenv("ACTION_DIST_CM", "30"))                # ultrasonic threshold (cm)
ACTION_BBOX_RATIO = float(os.getenv("ACTION_BBOX_RATIO", "0.50"))      # bbox height ratio (0.50 = 50% of frame)

# === Health check ===
HEALTH_CHECK_TIMEOUT = int(os.getenv("HEALTH_CHECK_TIMEOUT", "5"))
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "15"))


# === LLM Factory Functions ===

_cached_llm = None


def create_llm_for_model(model_string: str, temperature: float, max_tokens: int):
    """Instantiate a LangChain chat model from a 'provider/model_name' string."""
    if "/" in model_string:
        provider, model_name = model_string.split("/", 1)
    else:
        provider, model_name = "openai", model_string

    provider = provider.lower()

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model_name, temperature=temperature, max_tokens=max_tokens)
    elif provider == "azure":
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(deployment_name=model_name, api_version=AZURE_API_VERSION,
                               temperature=temperature, max_tokens=max_tokens)
    else:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model_name, temperature=temperature, max_tokens=max_tokens)


def create_llm():
    """Create or return cached LangChain LLM for Planner Agent."""
    global _cached_llm
    if _cached_llm is not None:
        return _cached_llm

    logger.info(f"Initializing Planner LLM: {LLM_MODEL}")
    _cached_llm = create_llm_for_model(LLM_MODEL, OPENAI_TEMPERATURE, LLM_MAX_TOKENS)
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
