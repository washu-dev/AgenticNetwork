"""
PiCrawler Thin Client config.

Only one value needs to be set per Pi (in .env):
  AGENT_ID  — unique robot identity
"""

from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent / ".env")

AGENT_ID = os.getenv("AGENT_ID", "crawler-alpha-patrol-004")
AGENT_PORT = int(os.getenv("AGENT_PORT", "9004"))
PATROL_SPEED = int(os.getenv("PATROL_SPEED", "50"))
OBSTACLE_THRESHOLD_CM = float(os.getenv("OBSTACLE_THRESHOLD_CM", "20.0"))
SPEAKER_ENABLED = os.getenv("SPEAKER_ENABLED", "false").lower() in ("true", "1", "yes")
