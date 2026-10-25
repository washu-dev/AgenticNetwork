"""agent_skeleton — the agent runtime for AgenticNetwork A2A agents.

Every hosted agent imports this package at run time. It holds:
  * the LLM tool loop (llm_loop.run_tool_loop) and AgentSpec (spec.py), which picks
    an agent's prompt + tools as DATA so one frozen loop serves every agent;
  * the A2A executor and server (executor.py, serve.py, a2a_runtime.py);
  * AgentHandler (base.py), the base class for custom agents, and HandlerExecutor
    (handler_executor.py), which serves one over A2A;
  * container_runner, the entry point of every built agent image;
  * system_tools/, ready-made tools (call_endpoint, the presets).

It also holds the wrapper-repo generator (generator/), which emits a complete,
deployable wrapper-agent folder from an answers.json, and a small demo agent
(demo/). The demo is safe to edit: nothing here imports it and it is not
installed; everything else in this package is runtime.

The import name ``agent_skeleton`` is a public interface: uploaded handlers import
it and built images run ``python -m agent_skeleton.container_runner``, so uploads
may not claim the name (registration_service/archive.py). Don't build an agent by
editing the runtime; a change there reaches every hosted agent. Register one of
three kinds instead (README.md, "Building an agent"; docs/BUILDING_AGENTS.md): a
custom AgentHandler, an ``llm`` agent with an uploaded tools module, or a no-code
proxy/http agent.

See README.md for the walkthrough and CLAUDE.md for working notes.

Exposed API:
    AgentSpec, endpoint_wrapper_spec          — the spec-driven engine
    EndpointConfig, make_call_endpoint        — call an external endpoint
    AgentHandler, FileInput, HandlerExecutor  — uploaded-code agents
        (custom-handler path via the registration service)
"""
from __future__ import annotations

from .spec import AgentSpec, endpoint_wrapper_spec
from .system_tools.call_endpoint import EndpointConfig, make_call_endpoint

__all__ = [
    "AgentSpec",
    "endpoint_wrapper_spec",
    "EndpointConfig",
    "make_call_endpoint",
]

# Custom-handler API for uploaded agents. Present in the full package; the lean
# copy the generator vendors into a wrapper agent omits base/handler_executor, so
# import these defensively.
try:
    from .base import AgentHandler, FileInput
    from .handler_executor import HandlerExecutor

    __all__ += ["AgentHandler", "FileInput", "HandlerExecutor"]
except ImportError:
    pass
