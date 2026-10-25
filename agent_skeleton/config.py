"""Shared configuration & constants.  ENGINE (defaults for every agent)

These values are not just the demo's. MAX_TOOL_STEPS is the step limit for every
AgentSpec that doesn't set its own, AGENT_NAME is the name in every
SkeletonAgentExecutor heartbeat ("... is working..."), and the env_* readers are
how a deployment sets host, port, model, ADS address and the wrapped endpoint
without code edits. Change a default only on purpose: it reaches every hosted
agent.
"""
from __future__ import annotations

import os

# --- Identity -------------------------------------------------------------
AGENT_NAME = "Endpoint Wrapper Agent"

# --- Networking -----------------------------------------------------------
DEFAULT_HOST = "0.0.0.0"          # bind address (listen on all interfaces)
DEFAULT_PORT = 9110               # pick a free port (specialist agents use 9103-9104, 19105-19108)

# --- LLM ------------------------------------------------------------------
DEFAULT_MODEL = "gpt-4o-mini"     # hosted model name, or your vLLM --served-model-name
MAX_TOOL_STEPS = 4                # cap on tool-call loop iterations (disaster uses 4)

# --- Directory (ADS) ------------------------------------------------------
DEFAULT_ADS_SERVER_ADDR = "127.0.0.1:8888"


# --- Env readers (so deployment can override without code edits) ----------
def env_model() -> str:
    return os.getenv("AGENT_MODEL", DEFAULT_MODEL)


def env_host() -> str:
    return os.getenv("AGENT_A2A_HOST", DEFAULT_HOST)


def env_port() -> int:
    raw = os.getenv("AGENT_A2A_PORT", str(DEFAULT_PORT))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"AGENT_A2A_PORT must be an integer, got {raw!r}") from exc


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


# --- Wrapped external endpoint (the API this agent fronts) ----------------
# The endpoint URL is not a secret (it goes in the public card); the auth TOKEN
# is read at call time from the env var NAMED by AGENT_ENDPOINT_AUTH_ENV, so no
# secret is ever stored in code, the card, or the registration record.
def env_endpoint_url() -> str | None:
    return os.getenv("AGENT_ENDPOINT_URL")


def env_endpoint_protocol() -> str:
    return os.getenv("AGENT_ENDPOINT_PROTOCOL", "http")  # 'http' | 'a2a'


def env_endpoint_method() -> str:
    return os.getenv("AGENT_ENDPOINT_METHOD", "POST")


def env_endpoint_auth_env() -> str | None:
    # The NAME of the env var holding the token (e.g. "MY_API_TOKEN"), not the token.
    return os.getenv("AGENT_ENDPOINT_AUTH_ENV")
