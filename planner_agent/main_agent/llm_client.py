from __future__ import annotations

import asyncio
import json
import logging
import os
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
    uploaded_files: list[dict[str, Any]] = Field(default_factory=list)
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

    It owns one AsyncOpenAI client per endpoint (primary, fallback) and event
    loop, reused for every request and closed by ``aclose()`` at app shutdown.
    No client is left to the garbage collector: the SDK's default HTTP client
    closes itself from ``__del__`` by scheduling ``aclose()`` on whatever loop is
    running when it is collected, and that late transport close can unregister
    the selector entry of an unrelated socket that has since been given the same
    file descriptor, such as a pending A2A connect, which then hangs until its
    timeout.
    """

    def __init__(self, config: MainAgentConfig, openai_client: Any | None = None):
        self.config = config
        self._client = openai_client
        # (event loop, provider, base_url, api_key, timeout) -> AsyncOpenAI.
        self._clients: dict[tuple[Any, ...], Any] = {}

    async def aclose(self) -> None:
        """Close the clients this instance opened on the running loop.

        Entries for loops that have already closed are forgotten: their
        connections cannot be closed from another loop.
        """
        loop = asyncio.get_running_loop()
        self._forget_closed_loops()
        mine = [key for key in self._clients if key[0] is loop]
        for key in mine:
            await self._clients.pop(key).close()

    def _forget_closed_loops(self) -> None:
        for key in [key for key in self._clients if key[0].is_closed()]:
            del self._clients[key]

    def _endpoint_client(self, provider: str, *, base_url: str | None, timeout: float) -> Any:
        """The AsyncOpenAI client for one endpoint on the running event loop.

        A client is bound to the loop it first ran on (its connections belong to
        that loop), so the cache is keyed by the loop as well as by everything
        the client is built from; ``base_url=None`` resolves from the environment
        as the SDK does. Vertex keys are short-lived OAuth tokens: a fresh one is
        minted per request and sent through ``with_options``, a copy that shares
        the cached client's connection pool.
        """
        loop = asyncio.get_running_loop()
        self._forget_closed_loops()
        vertex_token = _vertex_access_token() if provider == "vertex" else None
        key = (
            loop,
            provider,
            base_url or os.getenv("OPENAI_BASE_URL"),
            None if vertex_token is not None else os.getenv("OPENAI_API_KEY"),
            timeout,
        )
        client = self._clients.get(key)
        if client is None:
            client = _make_client(base_url=base_url, timeout=timeout, api_key=vertex_token)
            self._clients[key] = client
        elif vertex_token is not None:
            client = client.with_options(api_key=vertex_token)
        return client

    async def next_tool_call(self, state: TurnState) -> ToolCall:
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
            "messages": state.messages,
            "tools": _cached_tool_schemas(),
            "tool_choice": "required",
            "max_tokens": self.config.max_output_tokens,
        }

        for attempt in range(3):
            try:
                response = await self._create_with_failover(kwargs)
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
            getattr(response, "model", self.config.llm_model),
            finish_reason,
            usage,
        )
        call = _extract_tool_call_from_message(message)
        call.token_usage = _usage_to_dict(usage)
        if call.call_id:
            state.last_call_id = call.call_id
        return call

    async def _create_with_failover(self, base_kwargs: dict[str, Any]) -> Any:
        """Call the primary provider; on an availability error, retry on the fallback.

        Failover is per-request, so it also *recovers* automatically: every request
        tries the primary first, so once the primary is healthy again it's used again
        with no intervention. Only availability errors (connection/timeout/5xx) trigger
        failover — a 401 or a bad-request would fail on both and is surfaced as-is.
        """
        # An injected client (tests) is used verbatim, no failover.
        if self._client is not None:
            return await self._client.chat.completions.create(
                **{**base_kwargs, "model": self.config.llm_model}
            )

        has_fallback = bool(self.config.llm_fallback_provider)
        primary = self._endpoint_client(
            self.config.llm_provider,
            base_url=None,  # primary base_url/api_key come from the environment
            timeout=(
                self.config.llm_primary_timeout_seconds
                if has_fallback
                else self.config.timeout_seconds
            ),
        )
        try:
            return await primary.chat.completions.create(
                **{**base_kwargs, "model": self.config.llm_model}
            )
        except Exception as exc:
            if not has_fallback or not _is_availability_error(exc):
                raise
            logger.warning(
                "main_agent_llm_failover primary=%s err=%s -> fallback=%s",
                self.config.llm_provider,
                type(exc).__name__,
                self.config.llm_fallback_provider,
            )
            fallback = self._endpoint_client(
                self.config.llm_fallback_provider or "",
                base_url=self.config.llm_fallback_base_url,
                timeout=self.config.timeout_seconds,
            )
            return await fallback.chat.completions.create(
                **{**base_kwargs, "model": self.config.llm_fallback_model or self.config.llm_model}
            )


def _initial_payload(state: TurnState) -> dict[str, Any]:
    return {
        "user_text": state.user_text,
        "context_id": state.context_id,
        "conversation_history": state.conversation_history,
        "pending_confirmation": state.pending_confirmation,
        "uploaded_files": _uploaded_files_summary(state.uploaded_files),
        "instructions": (
            "Pick exactly one tool for this model step. The runtime will feed the observation "
            "back so you can take another step in the same user request. Prefer list_agents "
            "first when you don't yet know which agent fits, then continue to call_agent when "
            "a suitable agent exists. Use describe_agent only when capability detail is needed. "
            "If uploaded_files is non-empty, the user has attached a file. The runtime will forward "
            "the file bytes in the A2A call to whatever agent you invoke — you do not need to handle "
            "file transfer yourself. Choose the agent that best matches the user's intent: a geospatial "
            "agent for photos to analyze or localize, a coding agent for code/data files, an email agent "
            "to send the file as an attachment, etc. "
            "For compound requests, choose one primary related agent and send the whole task; "
            "external agents coordinate peer A2A calls when needed. "
            "Use final_answer only after the user request has been answered or delegated. "
            "When calling final_answer, always write a complete helpful response in the text argument — "
            "never say 'I am not sure' or leave it blank. If you just received a describe_agent or "
            "list_agents observation, summarize it in plain English as your final_answer text."
        ),
    }


def _uploaded_files_summary(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "filename": f.get("filename"),
            "content_type": f.get("content_type"),
            "size": f.get("size"),
        }
        for f in files
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


def _make_client(*, base_url: str | None, timeout: float, api_key: str | None = None) -> Any:
    """Build an AsyncOpenAI client.

    For ``vertex`` the caller passes ``api_key``: a freshly-minted, short-lived (~1h)
    OAuth token from the runtime service account (not a static key). When ``base_url``
    or ``api_key`` is None the SDK reads ``OPENAI_BASE_URL``/``OPENAI_API_KEY`` from the
    environment — used for the primary provider; the fallback passes ``base_url``
    explicitly so it can point somewhere other than the primary's env-configured endpoint.

    The HTTP client is a plain ``DefaultAsyncHttpxClient`` rather than the SDK's
    self-closing default (see ``OpenAIToolLoopClient``): it is closed explicitly, and
    one that is dropped never closes itself late on whatever loop is then running.
    """
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient

    kwargs: dict[str, Any] = {"timeout": timeout, "http_client": DefaultAsyncHttpxClient()}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key is not None:
        kwargs["api_key"] = api_key
    return AsyncOpenAI(**kwargs)


def _is_availability_error(exc: Exception) -> bool:
    """True for errors that mean the provider is *unreachable/unhealthy* (worth a
    failover), as opposed to auth (401) or client (4xx) errors that would fail on
    the fallback too."""
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return getattr(exc, "status_code", None) in {500, 502, 503, 504}
    return False


def _vertex_access_token() -> str:
    """Mint a fresh Google Cloud access token for the Vertex AI OpenAI-compatible API.

    Uses Application Default Credentials — on Cloud Run this resolves to the runtime
    service account via the metadata server (no key file). google-auth caches and
    refreshes the token, so calling this once per request is cheap and always-valid.
    """
    try:
        import google.auth
        import google.auth.transport.requests
    except Exception as exc:
        raise RuntimeError(
            "google-auth import failed (required for MAIN_AGENT_LLM_PROVIDER=vertex): "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    if not creds.token:
        raise RuntimeError("google.auth returned an empty Vertex access token")
    return creds.token


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
