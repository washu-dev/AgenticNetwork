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
    # Free-text reasoning summary from the model (when available, e.g. gpt-5.x
    # reasoning models with reasoning.summary='auto'). Surfaced to the UI as a
    # "thinking" step before the tool entry. Empty for non-reasoning models.
    reasoning: str | None = None


class TurnState(BaseModel):
    """LLM-visible state. Excludes raw endpoint URLs and credentials.

    Per-turn instance — never shared across requests/users. The OpenAI
    chaining ids live here (not on the client) so concurrent /messages
    requests cannot interleave each other's response chains.
    """

    context_id: str
    user_text: str
    step: int = 0
    conversation_history: list[str] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    uploaded_images: list[dict[str, Any]] = Field(default_factory=list)
    previous_response_id: str | None = None
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
    """OpenAI Responses-API tool-loop client.

    Stateless across turns: response-id chaining state lives on the per-turn
    ``TurnState`` (not on the client instance). This makes the client safe to
    share across concurrent /messages requests — two users cannot leak each
    other's response chain.
    """

    def __init__(self, config: MainAgentConfig, openai_client: Any | None = None):
        self.config = config
        self._client = openai_client

    async def next_tool_call(self, state: TurnState) -> ToolCall:
        client = self._client or _default_openai_client(self.config)

        kwargs: dict[str, Any] = {
            "model": self.config.llm_model,
            "tools": _cached_tool_schemas(),
            "tool_choice": "auto",
            "max_output_tokens": self.config.max_output_tokens,
        }
        if self.config.reasoning_effort:
            # summary='detailed' is the only value that reliably produces a
            # summary text for short prompts on gpt-5.1. 'auto' often returns
            # an empty summary list, 'concise' is also unreliable. We surface
            # this text to the UI so users can see "why" the LLM picked the
            # next tool. Pairs best with effort='medium'.
            kwargs["reasoning"] = {
                "effort": self.config.reasoning_effort,
                "summary": "detailed",
            }
        if self.config.verbosity:
            kwargs["text"] = {"verbosity": self.config.verbosity}

        if state.previous_response_id and state.last_call_id and state.observations:
            kwargs["previous_response_id"] = state.previous_response_id
            kwargs["input"] = [
                {
                    "type": "function_call_output",
                    "call_id": state.last_call_id,
                    "output": json.dumps(state.observations[-1], default=str),
                }
            ]
        else:
            kwargs["input"] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(_initial_payload(state), default=str),
                },
            ]

        try:
            response = await client.responses.create(**kwargs)
        except Exception as exc:
            logger.exception("main_agent_openai_failed model=%s", self.config.llm_model)
            raise RuntimeError(f"OpenAI request failed: {type(exc).__name__}: {exc}") from exc

        # Stash chain ids on the per-turn state, not on self.
        state.previous_response_id = getattr(response, "id", None) or (
            response.get("id") if isinstance(response, dict) else None
        )
        # Log token usage and incomplete-state so token-starvation truncation is visible.
        usage = _response_usage(response)
        incomplete = getattr(response, "incomplete_details", None) or (
            response.get("incomplete_details") if isinstance(response, dict) else None
        )
        status = getattr(response, "status", None) or (
            response.get("status") if isinstance(response, dict) else None
        )
        logger.info(
            "main_agent_openai_response context_id=%s model=%s status=%s incomplete=%s usage=%s",
            state.context_id,
            self.config.llm_model,
            status,
            incomplete,
            usage,
        )
        call = _extract_tool_call(response)
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
            "Use final_answer only after the user request has been answered or delegated."
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


def _extract_tool_call(response: Any) -> ToolCall:
    output = response.get("output") if isinstance(response, dict) else getattr(response, "output", None)
    reasoning_text = _extract_reasoning_summary(output)
    for item in output or []:
        item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        if item_type != "function_call":
            continue
        name = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
        raw_args = item.get("arguments") if isinstance(item, dict) else getattr(item, "arguments", "{}")
        call_id = item.get("call_id") if isinstance(item, dict) else getattr(item, "call_id", None)
        try:
            args = json.loads(raw_args or "{}") if isinstance(raw_args, str) else dict(raw_args or {})
        except json.JSONDecodeError:
            args = {}
        return ToolCall(
            tool_name=str(name),
            arguments=args,
            call_id=call_id,
            reasoning=reasoning_text,
        )
    return ToolCall(
        tool_name="final_answer",
        arguments={"text": _response_text(response)},
        reasoning=reasoning_text,
    )


def _response_usage(response: Any) -> Any | None:
    return getattr(response, "usage", None) or (
        response.get("usage") if isinstance(response, dict) else None
    )


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


def _extract_reasoning_summary(output: Any) -> str | None:
    """Return concatenated reasoning summary text from the response output.

    OpenAI reasoning models (gpt-5.x, o-series) emit items with type
    "reasoning" whose `summary` is a list of `summary_text` parts. Stitch them
    into one paragraph; return None if the model didn't emit any.
    """
    if not output:
        return None
    chunks: list[str] = []
    for item in output:
        item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        if item_type != "reasoning":
            continue
        summary = item.get("summary") if isinstance(item, dict) else getattr(item, "summary", None)
        for piece in summary or []:
            piece_type = piece.get("type") if isinstance(piece, dict) else getattr(piece, "type", None)
            if piece_type not in {"summary_text", "text"}:
                continue
            text = piece.get("text") if isinstance(piece, dict) else getattr(piece, "text", None)
            if text:
                chunks.append(str(text).strip())
        # Some models also expose .content fallback.
        if not chunks:
            content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
            for piece in content or []:
                text = piece.get("text") if isinstance(piece, dict) else getattr(piece, "text", None)
                if text:
                    chunks.append(str(text).strip())
    if not chunks:
        return None
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)
    if isinstance(response, dict):
        if response.get("output_text"):
            return str(response["output_text"])
        output = response.get("output")
    else:
        output = getattr(response, "output", None)
    chunks: list[str] = []
    for item in output or []:
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        for part in content or []:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if text:
                chunks.append(str(text))
    return "".join(chunks) or "I'm not sure how to answer that."


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
