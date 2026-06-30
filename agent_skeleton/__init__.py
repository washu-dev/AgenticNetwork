"""agent_skeleton — a minimal, copy-to-start template for an AgenticNetwork A2A agent.

Edit these to make your agent; everything else is plumbing:
    tool_schemas.py  — your tools' JSON schemas        (ZONE 1)
    prompt.py        — system prompt + result shape     (ZONE 2)
    tools.py         — your tool functions + registry   (ZONE 4)
    agent.card.json  — your skills & endpoint

See README.md for the walkthrough and CLAUDE.md for working notes.

Custom-handler API (for uploaded agents via the registration service):
    AgentHandler    — subclass and implement handle_structured()
    FileInput       — wrapper for binary inputs (PDFs, images)
    HandlerExecutor — A2A executor that wraps an AgentHandler
"""

from agent_skeleton.base import AgentHandler, FileInput
from agent_skeleton.handler_executor import HandlerExecutor

__all__ = ["__doc__", "AgentHandler", "FileInput", "HandlerExecutor"]
