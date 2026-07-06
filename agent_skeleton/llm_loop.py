"""ZONE 3 — The LLM tool-calling loop.  (COPY; rarely edit)

The generic Chat Completions tool loop, mirroring
disaster_response_agent.py:580-677. It is agent-agnostic: you give it a system
prompt, a user payload, the tool schemas, and a dispatch function.

WHY IT LOOKS LIKE THIS — vLLM / Chat Completions is STATELESS (there is no
`previous_response_id` like the old Responses API). So we hold the whole
conversation in `messages` locally and resend it every step. The required order
each round is:

    system -> user -> assistant(WITH tool_calls) -> tool result(s) -> ...

Appending the assistant message BEFORE the tool results is load-bearing.

WHY TOOL EXTRACTION IS DETERMINISTIC — we pass `tools=...`, so the model is
constrained by the API to emit any call as STRUCTURED JSON in
`response.choices[0].message.tool_calls` (id + function.name + function.arguments
as a JSON string). We read a typed field; we never regex-parse prose.
(Against vLLM this requires launching with `--enable-auto-tool-choice
--tool-call-parser <parser>`, or the model silently stops calling tools.)
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Callable

from .config import MAX_TOOL_STEPS, env_model

if TYPE_CHECKING:
    from .spec import AgentSpec


def build_openai_client() -> Any:
    """OpenAI Chat Completions client. Reads OPENAI_BASE_URL / OPENAI_API_KEY,
    so the same code works against hosted OpenAI or a self-hosted vLLM endpoint."""
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Missing OPENAI_API_KEY (use any non-empty placeholder for vLLM).")
    from openai import OpenAI

    return OpenAI(
        base_url=os.getenv("OPENAI_BASE_URL"),  # None => hosted OpenAI
        api_key=os.getenv("OPENAI_API_KEY", "dummy"),
    )


def _final_text(response: Any) -> str:
    # mirrors disaster's _response_text (438-445)
    try:
        content = response.choices[0].message.content
        return str(content).strip() if content else ""
    except (AttributeError, IndexError):
        return ""


def _extract_tool_calls(response: Any) -> list[Any]:
    # mirrors disaster's _extract_function_calls (454-459)
    try:
        tool_calls = response.choices[0].message.tool_calls
        return tool_calls if tool_calls else []
    except (AttributeError, IndexError):
        return []


def run_tool_loop(
    *,
    model: str,
    system_prompt: str,
    user_payload: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
    dispatch: Callable[[str, dict[str, Any]], dict[str, Any]],
    max_steps: int = MAX_TOOL_STEPS,
) -> tuple[str, list[dict[str, Any]]]:
    """Run the bounded tool-calling loop. Returns (final_text, tool_log)."""
    client = build_openai_client()
    messages: list[Any] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, indent=2)},
    ]
    request_kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if tool_schemas:
        request_kwargs["tools"] = tool_schemas
        request_kwargs["parallel_tool_calls"] = False

    response = client.chat.completions.create(**request_kwargs)
    tool_log: list[dict[str, Any]] = []

    for _ in range(max_steps):
        tool_calls = _extract_tool_calls(response)
        if not tool_calls:
            break  # the model is done; its content is the final answer

        # Record the assistant turn (WITH its tool_calls) BEFORE the results.
        messages.append(response.choices[0].message)

        for call in tool_calls:
            name = call.function.name
            call_id = call.id
            try:
                arguments = json.loads(call.function.arguments or "{}")
                result = dispatch(name, arguments)
            except Exception as exc:  # never let a tool crash the loop
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            tool_log.append({"name": name, "arguments": call.function.arguments, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result, default=str),
                }
            )

        request_kwargs["messages"] = messages
        response = client.chat.completions.create(**request_kwargs)

    return _final_text(response), tool_log


# --- High-level engine entry point (wires the AgentSpec into the loop) ----

def run_agent(
    user_payload: dict[str, Any],
    *,
    spec: "AgentSpec | None" = None,
    model: str | None = None,
) -> dict[str, Any]:
    """The agent's 'brain': run the loop with an AgentSpec's prompt + tools, then
    normalize. The executor (zone 5) calls this and knows nothing about A2A; this
    function knows nothing about A2A either.

    The ``spec`` is the seam that lets ONE engine serve many agents/levels without
    editing this file: it carries the system prompt, tool schemas, tool dispatch,
    and normalize. When ``spec`` is omitted, ``default_demo_spec()`` reproduces the
    original skeleton behavior (the demo word_count/reverse_text tools), so callers
    that predate the spec keep working unchanged. ``run_tool_loop`` above stays
    frozen and generic — every level flows through it."""
    from .spec import default_demo_spec

    spec = spec or default_demo_spec()
    final_text, tool_log = run_tool_loop(
        model=model or spec.model or env_model(),
        system_prompt=spec.system_prompt,
        user_payload=user_payload,
        tool_schemas=spec.tool_schemas,
        dispatch=spec.dispatch,
        max_steps=spec.max_steps,
    )
    return spec.run_normalize(final_text, tool_log)
