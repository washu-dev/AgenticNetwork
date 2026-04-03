"""
Command Center configuration.

Planner Agent + Robot Agent services.
"""

import os

# === Server ===
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8080"))

# === LLM ===
PI_BRAIN_LLM_MODEL = os.getenv("PI_BRAIN_LLM_MODEL", os.getenv("LLM_MODEL", "openai/gpt-4o-mini"))

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
MISSION_TIMEOUT_STATIONARY = int(os.getenv("MISSION_TIMEOUT_STATIONARY", "20"))
MISSION_TIMEOUT_PATROL = int(os.getenv("MISSION_TIMEOUT_PATROL", "180"))

# === Health check ===
HEALTH_CHECK_TIMEOUT = int(os.getenv("HEALTH_CHECK_TIMEOUT", "5"))
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "15"))
