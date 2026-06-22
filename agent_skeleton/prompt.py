"""ZONE 2 — System prompt + result normalization.  ★ WRITE THIS ★

- SYSTEM_PROMPT: the instructions that define your agent's behavior and the
  exact output contract you want from the model. Mirror
  disaster_response_agent.py:380-435.
- normalize_result(): turn the model's final text into the STABLE structured
  dict your agent returns. Mirror disaster's _normalize_plan (720-744).

Why a stable shape matters: the planner reads the structured DataPart artifact
your agent emits, so downstream callers depend on these keys always existing.
"""
from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = (
    "You are a helpful assistant agent. Use the available tools when they help "
    "answer the user's request, then produce a final answer. "
    "Return ONLY valid JSON with these keys: "
    'answer (string), tools_used (array of strings). '
    "Do not include any prose outside the JSON object."
)


def normalize_result(raw_text: str, tool_log: list[dict[str, Any]]) -> dict[str, Any]:
    """Coerce the model's final text into a stable result dict.

    Always returns the same keys (answer, tools_used, response_text) so callers
    can rely on them even if the model returns malformed JSON.
    """
    data: dict[str, Any] = {}
    text = (raw_text or "").strip()

    # Tolerate ```json fences around the JSON.
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            data = parsed
    except (ValueError, TypeError):
        data = {}

    answer = str(data.get("answer") or raw_text or "").strip() or "(no answer produced)"
    tools_used = data.get("tools_used")
    if not isinstance(tools_used, list):
        tools_used = [str(call.get("name")) for call in tool_log]

    return {
        "answer": answer,
        "tools_used": [str(t) for t in tools_used],
        # The executor uses response_text as the human-readable A2A message.
        "response_text": answer,
    }
