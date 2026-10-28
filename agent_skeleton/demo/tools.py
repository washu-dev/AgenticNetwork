"""The demo agent's tools: schemas, bodies and registry in one file.  DEMO AGENT

This is exactly the shape of an uploaded tools module (an ``llm`` agent with
``tools.source = "upload"``): module-level TOOL_SCHEMAS + TOOL_REGISTRY, no imports
from the runtime, so ``spec.load_tool_module`` loads this file unmodified.

TOOL_SCHEMAS is in OpenAI **Chat Completions** shape:

    {"type": "function",
     "function": {"name": ..., "description": ..., "parameters": <JSON Schema>}}

This is exactly the shape used by the specialist agents (e.g. disaster's
DISASTER_TOOL_SCHEMAS).

Three rules the startup check enforces (tools.validate_tool_registry):
  1. Every `name` in TOOL_SCHEMAS has a matching function in TOOL_REGISTRY.
  2. The `parameters` match that function's signature — each schema property is a
     keyword arg of the function; required properties may or may not have a
     default; OPTIONAL properties must have a default.
  3. The envelope: `"type": "function"`, and `parameters` with `"type": "object"`
     (a no-argument tool may omit `parameters`; a missing `type` only warns).

So the schema and the Python signature are two views of one thing. (If you ever
want this to be impossible to get wrong, generate these schemas FROM the typed
functions instead — see agent_skeleton/AGENTS.md §5.)

Adding a tool = add one schema + write the function (keyword args named exactly
like its schema `properties`, returning a JSON-able dict) + add one registry entry.
Then run ``python -m agent_skeleton.demo check``. A registry REPLACES the
hand-written if/elif chain in disaster's _run_tool; the LLM loop dispatches over a
spec's registry via ``AgentSpec.dispatch`` (see spec.py).
"""
from __future__ import annotations

from typing import Any, Callable

# --- Schemas (what the model sees) ----------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "word_count",
            "description": "Count the words and characters in a piece of text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to measure."},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reverse_text",
            "description": "Reverse a piece of text, optionally upper-casing the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to reverse."},
                    "uppercase": {
                        "type": "boolean",
                        "description": "If true, upper-case the reversed text.",
                    },
                },
                # 'uppercase' is intentionally NOT required -> its function
                # parameter must therefore carry a default. This exercises the
                # alignment check.
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
]


# --- Tool bodies -----------------------------------------------------------

def word_count(*, text: str) -> dict[str, Any]:
    return {"ok": True, "word_count": len(text.split()), "char_count": len(text)}


def reverse_text(*, text: str, uppercase: bool = False) -> dict[str, Any]:
    reversed_text = text[::-1]
    if uppercase:
        reversed_text = reversed_text.upper()
    return {"ok": True, "reversed": reversed_text}


# --- Registry (one entry per tool) -----------------------------------------

TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "word_count": word_count,
    "reverse_text": reverse_text,
}
