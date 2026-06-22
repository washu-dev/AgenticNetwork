"""Shared configuration & constants.

SUPPORT FILE — you normally only edit the DEFAULT_* values below.
Mirrors the constants block of disaster_response_agent.py:55-65.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Identity -------------------------------------------------------------
AGENT_NAME = "Skeleton Agent"

# --- Networking -----------------------------------------------------------
DEFAULT_HOST = "0.0.0.0"          # bind address (listen on all interfaces)
DEFAULT_PORT = 9110               # pick a free port (parent repo uses 9103-9106)
DEFAULT_AGENT_URL = "http://127.0.0.1:9110/"  # the URL advertised in the card

# --- LLM ------------------------------------------------------------------
DEFAULT_MODEL = "gpt-4o-mini"     # hosted model name, or your vLLM --served-model-name
MAX_TOOL_STEPS = 4                # cap on tool-call loop iterations (disaster uses 4)

# --- Directory (ADS) ------------------------------------------------------
DEFAULT_ADS_SERVER_ADDR = "127.0.0.1:8888"

# --- Paths ----------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CARD_PATH = PACKAGE_DIR / "agent.card.json"


# --- Env readers (so deployment can override without code edits) ----------
def env_model() -> str:
    return os.getenv("AGENT_MODEL", DEFAULT_MODEL)


def env_host() -> str:
    return os.getenv("AGENT_A2A_HOST", DEFAULT_HOST)


def env_port() -> int:
    return int(os.getenv("AGENT_A2A_PORT", str(DEFAULT_PORT)))


def env_advertise_url() -> str | None:
    return os.getenv("AGENT_A2A_URL")


def env_ads_addr() -> str:
    # The parent repo uses several near-duplicate var names; accept the common ones.
    return (
        os.getenv("ADS_SERVER_ADDR")
        or os.getenv("ADS_URL")
        or os.getenv("ADS_SERVER_ADDRESS")
        or DEFAULT_ADS_SERVER_ADDR
    )
