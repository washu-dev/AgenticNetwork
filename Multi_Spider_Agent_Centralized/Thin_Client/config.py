"""
Configuration for PiCrawler Thin Client.
No LLM settings — all intelligence runs on Command Center.
"""

from pathlib import Path
from dotenv import load_dotenv
import os
import socket

load_dotenv(Path(__file__).parent / ".env")

# Robot identity (change per Pi)
AGENT_ID = os.getenv("AGENT_ID", "crawler-alpha-patrol-001")


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


LOCAL_IP = os.getenv("LOCAL_IP", _get_local_ip())

# Ports
AGENT_PORT = int(os.getenv("AGENT_PORT", "9004"))      # A2A server
STREAM_PORT = int(os.getenv("STREAM_PORT", "9000"))     # MJPEG video stream
STREAM_BASE_URL = os.getenv("STREAM_BASE_URL", f"http://localhost:{STREAM_PORT}")

# Command Center (for heartbeat)
COMMAND_CENTER_URL = os.getenv("COMMAND_CENTER_URL", "http://10.229.117.154:8080")

# Speaker: TTS and sound effects (default OFF)
SPEAKER_ENABLED = os.getenv("SPEAKER_ENABLED", "false").lower() in ("true", "1", "yes")

# Calibration (measure and adjust per robot)
OBSTACLE_THRESHOLD_CM = float(os.getenv("OBSTACLE_THRESHOLD_CM", "20.0"))
PATROL_SPEED = int(os.getenv("PATROL_SPEED", "50"))

# ADS registration (empty = skip)
ADS_ADDRESS = os.getenv("ADS_ADDRESS", "")
OASF_ADDRESS = os.getenv("OASF_ADDRESS", "")

# A2A Transport (NATS or SLIM)
DEFAULT_MESSAGE_TRANSPORT = os.getenv("DEFAULT_MESSAGE_TRANSPORT", "NATS")
TRANSPORT_SERVER_ENDPOINT = os.getenv("TRANSPORT_SERVER_ENDPOINT", "nats://localhost:4222")
BROADCAST_TOPIC = os.getenv("BROADCAST_TOPIC", "agents.broadcast")
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "10"))
