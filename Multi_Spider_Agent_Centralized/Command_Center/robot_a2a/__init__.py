"""A2A server implementation for Robot Agents.

This package exposes each Robot Agent as a proper A2A server:
  - build_agent_card: produce an a2a.types.AgentCard
  - RobotAgentExecutor: handle A2A JSON-RPC requests by dispatching to
    the existing RobotAgentService / CentralPiAgent logic
  - a2a_http_port: deterministic HTTP port per robot

Task req/reply flows through A2A JSON-RPC over HTTP.  NATS is still
used for fire-and-forget reasoning/state pub/sub to the gateway.
"""

from .card import build_agent_card
from .ports import a2a_http_port

__all__ = ["build_agent_card", "a2a_http_port"]
