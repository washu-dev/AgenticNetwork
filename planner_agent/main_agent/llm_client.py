from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from pydantic import BaseModel, Field

from planner_agent.main_agent.config import MainAgentConfig
from planner_agent.main_agent.tools import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class ToolCall(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None
    token_usage: dict[str, Any] | None = None
    reasoning: str | None = None


class TurnState(BaseModel):
    """LLM-visible state. Excludes raw endpoint URLs and credentials.

    Per-turn instance — never shared across requests/users. The full Chat
    Completions message history lives here (not on the client) so concurrent
    /messages requests cannot interleave each other's conversation state.
    """

    context_id: str
    user_text: str
    step: int = 0
    conversation_history: list[str] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    uploaded_images: list[dict[str, Any]] = Field(default_factory=list)
    # Accumulated Chat Completions message history for this turn.
    messages: list[dict[str, Any]] = Field(default_factory=list)
    last_call_id: str | None = None


class LLMClient(Protocol):
    async def next_tool_call(self, state: TurnState) -> ToolCall: ...


_SCHEMAS_CACHE: list[dict[str, Any]] | None = None


def _cached_tool_schemas() -> list[dict[str, Any]]:
    global _SCHEMAS_CACHE
    if _SCHEMAS_CACHE is None:
        from planner_agent.main_agent.tools import tool_schemas
        _SCHEMAS_CACHE = tool_schemas()
    return _SCHEMAS_CACHE


class OpenAIToolLoopClient:
    """Chat Completions tool-loop client.

    Stateless across turns: the full message history accumulates on the
    per-turn TurnState, making this safe to share across concurrent requests.
    """

    def __init__(self, config: MainAgentConfig, openai_client: Any | None = None):
        self.config = config
        self._client = openai_client

    async def next_tool_call(self, state: TurnState) -> ToolCall:
        client = self._client or _default_openai_client(self.config)

        if not state.messages:
            # First step: initialise the conversation history.
            state.messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(_initial_payload(state), default=str),
                },
            ]
        elif state.observations and state.last_call_id:
            # Subsequent step: feed the tool result back as a tool message.
            state.messages.append({
                "role": "tool",
                "tool_call_id": state.last_call_id,
                "content": json.dumps(state.observations[-1], default=str),
            })

        kwargs: dict[str, Any] = {
            "model": self.config.llm_model,
            "messages": state.messages,
            "tools": _cached_tool_schemas(),
            "tool_choice": "required",
            "max_tokens": self.config.max_output_tokens,
        }

        for attempt in range(3):
            try:
                response = await client.chat.completions.create(**kwargs)
            except Exception as exc:
                logger.exception("main_agent_openai_failed model=%s", self.config.llm_model)
                raise RuntimeError(f"OpenAI request failed: {type(exc).__name__}: {exc}") from exc

            message = response.choices[0].message
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            has_tool_calls = bool(getattr(message, "tool_calls", None))
            if finish_reason == "stop" and not has_tool_calls and attempt < 2:
                logger.warning(
                    "main_agent_stop_no_tool_call_retrying context_id=%s attempt=%d",
                    state.context_id,
                    attempt,
                )
                continue
            break

        # Append the assistant turn to history so the next step has full context.
        state.messages.append(_message_to_dict(message))

        usage = getattr(response, "usage", None)
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        logger.info(
            "main_agent_openai_response context_id=%s model=%s finish_reason=%s usage=%s",
            state.context_id,
            self.config.llm_model,
            finish_reason,
            usage,
        )
        call = _extract_tool_call_from_message(message)
        call.token_usage = _usage_to_dict(usage)
        if call.call_id:
            state.last_call_id = call.call_id
        return call


def _initial_payload(state: TurnState) -> dict[str, Any]:
    return {
        "user_text": state.user_text,
        "context_id": state.context_id,
        "conversation_history": state.conversation_history,
        "pending_confirmation": state.pending_confirmation,
        "uploaded_images": _uploaded_image_summary(state.uploaded_images),
        "instructions": (
            "Pick exactly one tool for this model step. The runtime will feed the observation "
            "back so you can take another step in the same user request. Prefer list_agents "
            "first when you don't yet know which agent fits, then continue to call_agent when "
            "a suitable agent exists. Use describe_agent only when capability detail is needed. "
            "If uploaded_images is non-empty, choose an image/geospatial-capable agent when the "
            "user asks to analyze, localize, or explain the photo; the runtime will attach the "
            "actual image bytes to that geospatial A2A call. "
            "For compound requests, choose one primary related agent and send the whole task; "
            "external agents coordinate peer A2A calls when needed. "
            "Use final_answer only after the user request has been answered or delegated. "
            "When calling final_answer, always write a complete helpful response in the text argument — "
            "never say 'I am not sure' or leave it blank. If you just received a describe_agent or "
            "list_agents observation, summarize it in plain English as your final_answer text."
        ),
    }


def _uploaded_image_summary(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "filename": image.get("filename"),
            "content_type": image.get("content_type"),
            "size": image.get("size"),
        }
        for image in images
    ]


def _message_to_dict(message: Any) -> dict[str, Any]:
    """Convert a Chat Completions message object to a plain dict for history."""
    msg: dict[str, Any] = {"role": "assistant"}
    content = getattr(message, "content", None)
    if content:
        msg["content"] = content
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]
    return msg


def _extract_tool_call_from_message(message: Any) -> ToolCall:
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        tc = tool_calls[0]
        name = tc.function.name
        raw_args = tc.function.arguments
        call_id = tc.id
        try:
            args = json.loads(raw_args or "{}") if isinstance(raw_args, str) else dict(raw_args or {})
        except json.JSONDecodeError:
            args = {}
        return ToolCall(tool_name=str(name), arguments=args, call_id=call_id)
    content = getattr(message, "content", None)
    text = str(content).strip() if content else "I'm not sure how to answer that."
    return ToolCall(tool_name="final_answer", arguments={"text": text})


def _response_usage(response: Any) -> Any | None:
    return getattr(response, "usage", None)


def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
    if not usage:
        return None
    if hasattr(usage, "model_dump"):
        data = usage.model_dump(mode="json", exclude_none=True)
        return data if isinstance(data, dict) else None
    if isinstance(usage, dict):
        return {str(key): _jsonable_usage_value(value) for key, value in usage.items() if value is not None}

    data: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
    ):
        value = getattr(usage, key, None)
        if value is not None:
            data[key] = value
    return data or None


def _jsonable_usage_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {str(key): _jsonable_usage_value(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_jsonable_usage_value(item) for item in value]
    return value


def _default_openai_client(config: MainAgentConfig) -> Any:
    try:
        from openai import AsyncOpenAI
    except Exception as exc:
        raise RuntimeError(f"openai SDK import failed: {type(exc).__name__}: {exc}") from exc
    return AsyncOpenAI(timeout=config.timeout_seconds)


class ScriptedLLMClient:
    """Mock LLM client for tests. Returns scripted ToolCall sequence."""

    def __init__(self, calls: list[ToolCall]):
        self._calls = list(calls)
        self.states: list[TurnState] = []

    async def next_tool_call(self, state: TurnState) -> ToolCall:
        self.states.append(state.model_copy(deep=True))
        if not self._calls:
            return ToolCall(tool_name="final_answer", arguments={"text": "(scripted-end)"})
        return self._calls.pop(0)
