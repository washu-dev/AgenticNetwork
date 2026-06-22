"""ZONE 1 — Tool schemas.  ★ WRITE THIS ★

The list of tools your LLM may call, in OpenAI **Chat Completions** shape:

    {"type": "function",
     "function": {"name": ..., "description": ..., "parameters": <JSON Schema>}}

This is exactly the shape used by disaster_response_agent.py:288-367
(DISASTER_TOOL_SCHEMAS).

Two rules the startup check enforces (tools.validate_tool_registry):
  1. Every `name` here has a matching function in tools.py's TOOL_REGISTRY.
  2. The `parameters` here match that function's signature — each schema
     property is a keyword arg of the function; required properties may or may
     not have a default; OPTIONAL properties must have a default.

So the schema and the Python signature are two views of one thing. (If you ever
want this to be impossible to get wrong, generate these schemas FROM the typed
functions instead — see CLAUDE.md "Closing the gap further".)
"""
from __future__ import annotations

from typing import Any

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
