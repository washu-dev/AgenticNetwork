"""agent_skeleton — a minimal, copy-to-start template for an AgenticNetwork A2A agent.

Two ways to build on this skeleton:

1. Stock template — edit these; everything else is plumbing:
       tool_schemas.py  — your tools' JSON schemas        (ZONE 1)
       prompt.py        — system prompt + result shape     (ZONE 2)
       tools.py         — your tool functions + registry   (ZONE 4)
       agent.card.json  — your skills & endpoint

2. Endpoint wrapper — front an external API with an LLM loop; no code, just a
   card + an env var:
       AGENT_ENDPOINT_URL=https://api.example.com/run \\
       AGENT_ENDPOINT_AUTH_ENV=EXAMPLE_TOKEN \\
       python -m agent_skeleton.serve serve-wrapper --card my.card.json
   The AgentSpec seam (spec.py) selects an agent's prompt + tools as DATA, so one
   frozen engine (llm_loop.run_tool_loop) serves every configuration; system_tools/
   provides ready-made tools (today: call_endpoint). The generator/ package emits a
   complete, deployable wrapper-agent folder from an answers.json.

See README.md for the walkthrough and CLAUDE.md for working notes.

Exposed API:
    AgentSpec, default_demo_spec, endpoint_wrapper_spec  — the spec-driven engine
    EndpointConfig, make_call_endpoint                   — call an external endpoint
    AgentHandler, FileInput, HandlerExecutor             — uploaded-code agents
        (custom-handler path via the registration service)
"""
from __future__ import annotations

from .spec import AgentSpec, default_demo_spec, endpoint_wrapper_spec
from .system_tools.call_endpoint import EndpointConfig, make_call_endpoint

__all__ = [
    "AgentSpec",
    "default_demo_spec",
    "endpoint_wrapper_spec",
    "EndpointConfig",
    "make_call_endpoint",
]

# Custom-handler API for uploaded agents. Present in the full skeleton; the lean
# copy the generator vendors into a wrapper agent omits base/handler_executor, so
# import these defensively.
try:
    from .base import AgentHandler, FileInput
    from .handler_executor import HandlerExecutor

    __all__ += ["AgentHandler", "FileInput", "HandlerExecutor"]
except ImportError:
    pass
