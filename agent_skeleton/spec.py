"""AgentSpec — the data-driven description of ONE agent's brain.

This is the seam that lets a SINGLE engine (``llm_loop.run_tool_loop``) serve many
agents and control levels **without per-agent edits to the loop**. An ``AgentSpec``
bundles the "write" zones — system prompt, tool schemas, tool bodies, normalize —
into one object that the executor and serve layers pass straight through. The loop
core stays frozen and generic; only the spec changes.

Why this matters for the bigger picture: a generator can emit a *config* (an
``AgentSpec``) instead of editing ``llm_loop.py``, so we never recreate the
"~5 divergent copies of the engine" problem the parent repo already suffers from.

Presets included here:
  * ``default_demo_spec()`` — reproduces the original skeleton behavior (the demo
    word_count / reverse_text tools), so anything constructed without a spec keeps
    working unchanged.
  * ``endpoint_wrapper_spec(...)`` — THE end goal: an LLM loop whose only tool is
    ``call_endpoint``, i.e. an agent that wraps an external API. The model
    translates the task into a call, reads the response, and answers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from .config import MAX_TOOL_STEPS

if TYPE_CHECKING:
    from .system_tools.call_endpoint import EndpointConfig

# A tool body: keyword args named exactly like its schema properties -> JSON-able dict.
ToolFn = Callable[..., dict[str, Any]]
# (raw_text, tool_log) -> stable result dict.
NormalizeFn = Callable[[str, list[dict[str, Any]]], dict[str, Any]]


@dataclass
class AgentSpec:
    """Everything the engine needs to run one agent, as data rather than code.

    ``mode`` is informational — it records which "bones" this agent uses so a
    generator/registration UI can reason about control levels:
      * ``"llm"``    — LLM tool loop (the default; includes the endpoint wrapper).
      * ``"proxy"``  — no LLM; relay straight to an upstream (reserved; see README).
      * ``"custom"`` — a hand-written executor owns the flow (reserved).
    """

    name: str
    system_prompt: str
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    tool_registry: dict[str, ToolFn] = field(default_factory=dict)
    normalize: NormalizeFn | None = None
    model: str | None = None
    max_steps: int = MAX_TOOL_STEPS
    mode: str = "llm"

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The dispatcher handed to ``run_tool_loop`` — a dict lookup over this
        spec's registry (replaces disaster's if/elif tool chain)."""
        fn = self.tool_registry.get(name)
        if fn is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        return fn(**arguments)

    def run_normalize(self, raw_text: str, tool_log: list[dict[str, Any]]) -> dict[str, Any]:
        if self.normalize is not None:
            return self.normalize(raw_text, tool_log)
        from .prompt import normalize_result

        return normalize_result(raw_text, tool_log)

    def validate(self) -> None:
        """Fail fast if this spec's schemas and functions disagree. Reuses the
        same alignment check the original skeleton runs at startup."""
        from .tools import validate_tool_registry

        validate_tool_registry(self.tool_schemas, self.tool_registry)


def default_demo_spec() -> AgentSpec:
    """Reproduce the original skeleton behavior from the existing module globals.

    Keeps backward compatibility: ``run_agent`` / ``create_app`` with no spec
    behave exactly as the stock template did."""
    from .config import AGENT_NAME
    from .prompt import SYSTEM_PROMPT, normalize_result
    from .tool_schemas import TOOL_SCHEMAS
    from .tools import TOOL_REGISTRY

    return AgentSpec(
        name=AGENT_NAME,
        system_prompt=SYSTEM_PROMPT,
        tool_schemas=list(TOOL_SCHEMAS),
        tool_registry=dict(TOOL_REGISTRY),
        normalize=normalize_result,
        mode="llm",
    )


def llm_wrapper_spec(
    *,
    name: str,
    system_prompt: str,
    preset_tools: list[str] | None = None,
    model: str | None = None,
    max_steps: int = MAX_TOOL_STEPS,
    extra_schemas: list[dict[str, Any]] | None = None,
    extra_registry: dict[str, ToolFn] | None = None,
) -> AgentSpec:
    """Build a plain LLM-wrapper spec: the creator's OWN system prompt, optionally
    armed with PRESET TOOLS chosen by name from the built-in catalog
    (``system_tools/preset_tools``).

    This is the "LLM Agent" creation option, routed through the same frozen engine
    as everything else. With no tools it is a single LLM turn; with tools the loop
    lets the model call them and then answer. Unlike ``endpoint_wrapper_spec`` the
    prompt is free-form (the creator defines the persona/task), so we do NOT impose
    the endpoint wrapper's strict JSON contract — ``normalize_result`` falls back to
    the model's raw text as ``answer`` when it doesn't emit JSON, so any creator
    prompt keeps working. ``extra_*`` allow adding more system tools alongside the
    presets (same seam as ``endpoint_wrapper_spec``).

    Validated before return, so an unknown preset-tool name or a misaligned schema
    fails here rather than at serve time."""
    from .system_tools.preset_tools import resolve_preset_tools

    schemas, registry = resolve_preset_tools(preset_tools or [])
    schemas = [*schemas, *(extra_schemas or [])]
    registry = {**registry, **(extra_registry or {})}

    prompt = system_prompt
    if schemas:
        # Nudge the model to use the tools without hijacking the creator's prompt.
        prompt = (
            f"{system_prompt}\n\n"
            "You have tools available. Call them when they help you answer, then "
            "give your final answer."
        )

    spec = AgentSpec(
        name=name,
        system_prompt=prompt,
        tool_schemas=schemas,
        tool_registry=registry,
        model=model,
        max_steps=max_steps,
        mode="llm",
    )
    spec.validate()
    return spec


def endpoint_wrapper_spec(
    *,
    name: str,
    endpoint: "EndpointConfig",
    description: str = "",
    io_criteria: str = "",
    system_prompt: str | None = None,
    model: str | None = None,
    max_steps: int = MAX_TOOL_STEPS,
    extra_schemas: list[dict[str, Any]] | None = None,
    extra_registry: dict[str, ToolFn] | None = None,
) -> AgentSpec:
    """Build the LLM-endpoint-wrapper spec: an LLM loop over a single
    ``call_endpoint`` tool bound to ``endpoint``.

    This is the concrete instantiation of the end goal — "wrap an external API in
    an LLM loop." The endpoint URL/protocol/auth are fixed by ``endpoint`` (not
    chosen by the model); the model only decides *what* to send. ``extra_*`` let a
    caller add more pre-built/system tools (e.g. future network tools) alongside
    ``call_endpoint``.

    The spec is validated before return, so a misaligned tool fails here rather
    than at serve time."""
    from .prompt import build_wrapper_prompt
    from .system_tools.call_endpoint import make_call_endpoint

    schema, fn = make_call_endpoint(endpoint)
    schemas = [schema, *(extra_schemas or [])]
    registry: dict[str, ToolFn] = {"call_endpoint": fn, **(extra_registry or {})}

    prompt = system_prompt or build_wrapper_prompt(
        name=name,
        description=description,
        endpoint=endpoint,
        io_criteria=io_criteria,
    )

    spec = AgentSpec(
        name=name,
        system_prompt=prompt,
        tool_schemas=schemas,
        tool_registry=registry,
        model=model,
        max_steps=max_steps,
        mode="llm",
    )
    spec.validate()
    return spec
