"""System tools — reusable tool bones the platform provides to generated agents.

Unlike the zone-4 tool BODIES in ``tools.py`` (which are the agent author's own
capabilities), these are pre-built tools that every wrapper/loop agent can be
given by selecting them in an ``AgentSpec``. They are the "tools to talk to the
network and the endpoint" the system owns.

Today this package ships ``call_endpoint`` (reach the external service an agent
wraps). Network-discovery tools (list_agents / describe_agent / call_agent,
backed by ADS + A2A) are a planned addition; they require the directory stack and
so live behind their own phase.

Design rules for anything added here:
  * Each tool is a (schema, fn) pair where the fn's keyword params match the
    schema properties exactly, so ``tools.validate_tool_registry`` still guards
    it. Do NOT use ``**kwargs`` — that opts out of the alignment safety net.
  * Keep dependencies minimal (stdlib where possible) so a generated agent can
    run the tool without installing httpx / a2a-sdk.
  * Secrets are read from the environment at call time, never taken from the
    model and never written to disk.
"""
from __future__ import annotations

from .call_endpoint import (
    CALL_ENDPOINT_SCHEMA_TEMPLATE,
    EndpointConfig,
    make_call_endpoint,
)

__all__ = [
    "CALL_ENDPOINT_SCHEMA_TEMPLATE",
    "EndpointConfig",
    "make_call_endpoint",
]
