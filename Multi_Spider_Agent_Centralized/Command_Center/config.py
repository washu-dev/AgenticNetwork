"""
Configuration for Multi_Spider_Agent_Command_Center.

All intelligence centralized: planner + pi agent brains + detection.
Robots discovered via ADS or static config, communicate via A2A protocol.
"""

import os

# === Server ===
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8080"))

# === LLM (Planner - LangChain structured output) ===
# Configured via llm_config.py (reads LLM_MODEL, OPENAI_API_KEY, etc.)

# === LLM (Pi Agent Brain - raw HTTP) ===
PI_BRAIN_LLM_MODEL = os.getenv("PI_BRAIN_LLM_MODEL", os.getenv("LLM_MODEL", "openai/gpt-4o-mini"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# === Robot discovery ===
# "static" = use PI_AGENTS list below; "ads" = query Agent Directory Service
DISCOVERY_MODE = os.getenv("DISCOVERY_MODE", "static")
ADS_SERVER_ADDRESS = os.getenv("ADS_SERVER_ADDRESS", "localhost:8888")

# Static agent list (used when DISCOVERY_MODE="static")
# Format: [{"id": "agent-id", "host": "ip", "port": 9004}]
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

# === A2A Transport ===
# "NATS" or "SLIM" — matches AgenticNetwork's transport abstraction
DEFAULT_MESSAGE_TRANSPORT = os.getenv("DEFAULT_MESSAGE_TRANSPORT", "NATS")
TRANSPORT_SERVER_ENDPOINT = os.getenv("TRANSPORT_SERVER_ENDPOINT", "nats://localhost:4222")
# Broadcast topic for NATS mode (SLIM uses group sessions instead)
BROADCAST_TOPIC = os.getenv("BROADCAST_TOPIC", "agents.broadcast")
A2A_TIMEOUT_SECONDS = int(os.getenv("A2A_TIMEOUT_SECONDS", "120"))

# === Detection engine ===
FRAME_INTERVAL = float(os.getenv("FRAME_INTERVAL", "0.5"))
MISSION_TIMEOUT_STATIONARY = int(os.getenv("MISSION_TIMEOUT_STATIONARY", "20"))
MISSION_TIMEOUT_PATROL = int(os.getenv("MISSION_TIMEOUT_PATROL", "180"))
OBSERVATION_PUSH_INTERVAL = float(os.getenv("OBSERVATION_PUSH_INTERVAL", "2.0"))

# === Timeouts ===
HEALTH_CHECK_TIMEOUT = int(os.getenv("HEALTH_CHECK_TIMEOUT", "5"))

# === Robot registry ===
HEARTBEAT_TIMEOUT = int(os.getenv("HEARTBEAT_TIMEOUT", "30"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "15"))
