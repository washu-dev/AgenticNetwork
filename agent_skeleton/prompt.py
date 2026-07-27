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


# --- Endpoint-wrapper prompt (built from the agent card + endpoint answers) ---

def build_wrapper_prompt(
    *,
    name: str,
    description: str = "",
    endpoint: Any = None,
    io_criteria: str = "",
) -> str:
    """Assemble the system prompt for an LLM-endpoint-wrapper agent.

    The prompt is generated from the same answers the user gives when filling out
    the agent card (name, description) plus the endpoint protocol and any I/O
    criteria — so describing the agent IS configuring its behavior. The output
    contract MUST stay in lockstep with ``normalize_result`` above (answer +
    tools_used), or the normalized result will silently fall back to raw text."""
    protocol = getattr(endpoint, "protocol", "http") if endpoint is not None else "http"
    proto_label = "an A2A agent (JSON-RPC)" if protocol == "a2a" else "an HTTP/JSON API"

    lines = [f"You are '{name}', an agent that wraps an external service."]
    if description:
        lines.append(f"Service description: {description}")
    lines.append(
        f"The service is reachable ONLY through the `call_endpoint` tool, which calls "
        f"{proto_label}. You cannot reach it any other way."
    )
    lines.append(
        "To answer a request: (1) translate the user's task into a clear `request` "
        "string (and optional structured `payload`) for the service; (2) call "
        "`call_endpoint`; (3) read the response and call again if you need to refine "
        "or gather more; (4) produce a final answer grounded in what the service returned."
    )
    if io_criteria:
        lines.append(f"Input/output criteria for this service: {io_criteria}")
    lines.append(
        "Return ONLY valid JSON with these keys: answer (string), "
        "tools_used (array of strings). Do not include any prose outside the JSON object."
    )
    return "\n".join(lines)


def build_operations_prompt(
    *,
    name: str,
    description: str = "",
    tools: list[tuple[str, Any]] | None = None,
) -> str:
    """Assemble the system prompt for a TYPED per-operation wrapper agent.

    ``tools`` is a list of ``(tool_name, OperationConfig)`` — one per API operation.
    Each is listed with its HTTP method + path + description so the model picks the
    right operation and fills its TYPED arguments (each tool's schema declares them).
    The output contract stays identical to ``build_wrapper_prompt`` so
    ``normalize_result`` reads it the same way."""
    tools = tools or []
    lines = [
        f"You are '{name}', an agent that wraps an external API by exposing each of "
        "its operations as a separate tool."
    ]
    if description:
        lines.append(f"Overall description: {description}")
    lines.append(
        "Call the operation whose description best matches the request, filling in "
        "its typed arguments. You may call several, or the same one repeatedly to "
        "refine. Available operations:"
    )
    for tool_name, op in tools:
        method = getattr(op, "method", "GET")
        path = getattr(op, "path", "")
        detail = getattr(op, "description", "") or "(no description provided)"
        lines.append(f"  - `{tool_name}` — {method} {path}: {detail}")
    lines.append(
        "To answer a request: (1) choose the right operation tool(s); (2) fill in "
        "its typed arguments from the user's task; (3) call it; (4) read the response "
        "and call again if you need to refine or gather more; (5) produce a final "
        "answer grounded in what the API returned."
    )
    lines.append(
        "Return ONLY valid JSON with these keys: answer (string), "
        "tools_used (array of strings). Do not include any prose outside the JSON object."
    )
    return "\n".join(lines)


def build_manager_prompt(
    *,
    name: str,
    description: str = "",
    tools: list[tuple[str, Any]] | None = None,
) -> str:
    """Assemble the system prompt for a multi-endpoint MANAGER agent.

    ``tools`` is a list of ``(tool_name, EndpointConfig)`` — one per wrapped
    endpoint. The prompt lists each endpoint tool with its protocol and
    description so the model can ROUTE a request to the right endpoint (it may
    call more than one, or the same one repeatedly to refine). The output
    contract stays identical to ``build_wrapper_prompt`` so ``normalize_result``
    reads it the same way."""
    tools = tools or []
    lines = [
        f"You are '{name}', a manager agent that routes each request to the most "
        "appropriate external service among the ones you wrap."
    ]
    if description:
        lines.append(f"Overall description: {description}")
    lines.append(
        "You can reach these services ONLY through the tools below. Pick the tool "
        "whose description best matches the request; you may call several, or the "
        "same one repeatedly to refine. Available endpoint tools:"
    )
    for tool_name, cfg in tools:
        protocol = getattr(cfg, "protocol", "http")
        proto_label = "A2A/JSON-RPC" if protocol == "a2a" else "HTTP/JSON"
        label = getattr(cfg, "name", "") or tool_name
        detail = getattr(cfg, "description", "") or "(no description provided)"
        lines.append(f"  - `{tool_name}` — {label} [{proto_label}]: {detail}")
    lines.append(
        "To answer a request: (1) choose the right endpoint tool(s); (2) translate "
        "the user's task into a clear `request` string (and optional structured "
        "`payload`); (3) call the tool; (4) read the response and call again (any "
        "tool) if you need to refine or gather more; (5) produce a final answer "
        "grounded in what the services returned."
    )
    lines.append(
        "Return ONLY valid JSON with these keys: answer (string), "
        "tools_used (array of strings). Do not include any prose outside the JSON object."
    )
    return "\n".join(lines)
