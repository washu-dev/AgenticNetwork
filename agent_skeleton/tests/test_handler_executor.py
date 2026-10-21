"""HandlerExecutor.execute: how the request context reaches handle_structured, that
a sync handle_structured runs off the event loop, and how a result without a
string answer or a handler that can't be called is reported.

Each test drives execute() end to end with an in-memory event queue and request
context, then reads the final task state off the emitted status events. No
network. Skipped when a2a-sdk isn't installed (the executor requires it to serve).
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("a2a")

from a2a.types import TaskState, TaskStatusUpdateEvent  # noqa: E402

from agent_skeleton.base import AgentHandler  # noqa: E402
from agent_skeleton.handler_executor import HandlerExecutor  # noqa: E402

CREDENTIALS = {"example_service": {"api_key": "TEST_KEY_NOT_REAL"}}
EXPECTED_CONTEXT = {"credentials": CREDENTIALS, "user_id": "user@example.test"}


class _Queue:
    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


class _Ctx:
    """RequestContext stand-in: only the attributes execute() reads."""

    def __init__(self) -> None:
        self.task_id = "task-1"
        self.context_id = "ctx-1"
        self.message = None
        self.metadata = {"credentials": CREDENTIALS, "user_id": "user@example.test"}

    def get_user_input(self) -> str:
        return "hi"


def _run(handler: AgentHandler, *, heartbeat_seconds: float = 5.0) -> tuple[TaskState, str]:
    """Execute one request; return the final task state and its message text."""
    state, text, _ = _run_with_events(handler, heartbeat_seconds=heartbeat_seconds)
    return state, text


def _run_with_events(
    handler: AgentHandler, *, heartbeat_seconds: float = 5.0
) -> tuple[TaskState, str, list]:
    queue = _Queue()
    executor = HandlerExecutor(
        handler, heartbeat_seconds=heartbeat_seconds, max_runtime_seconds=30
    )
    asyncio.run(executor.execute(_Ctx(), queue))
    final = [e for e in queue.events if isinstance(e, TaskStatusUpdateEvent) and e.final]
    assert len(final) == 1, queue.events
    status = final[0].status
    text = " ".join(p.root.text for p in status.message.parts) if status.message else ""
    return status.state, text, queue.events


# ── the context argument ─────────────────────────────────────────────────────
# Each handler records the context it was given (None when it takes none).

class _KeywordOnly(AgentHandler):
    async def handle_structured(self, text, files=None, *, context=None):
        self.seen = context
        return {"answer": "ok"}


class _VarKeyword(AgentHandler):
    async def handle_structured(self, text, files=None, **kwargs):
        self.seen = kwargs.get("context")
        return {"answer": "ok"}


class _ThirdPositional(AgentHandler):
    async def handle_structured(self, text, files, ctx, /):
        self.seen = ctx
        return {"answer": "ok"}


class _ThirdPositionalAndVarKeyword(AgentHandler):
    async def handle_structured(self, text, files=None, ctx=None, **kwargs):
        self.seen = ctx
        return {"answer": "ok"}


class _PositionalOnlyContextAndVarKeyword(AgentHandler):
    async def handle_structured(self, text, files, context=None, /, **kwargs):
        self.seen = context
        return {"answer": "ok"}


class _NoContext(AgentHandler):
    async def handle_structured(self, text, files=None):
        self.seen = None
        return {"answer": "ok"}


@pytest.mark.parametrize(
    "handler_cls, expected",
    [
        (_KeywordOnly, EXPECTED_CONTEXT),
        (_VarKeyword, EXPECTED_CONTEXT),
        (_ThirdPositional, EXPECTED_CONTEXT),
        (_ThirdPositionalAndVarKeyword, EXPECTED_CONTEXT),
        (_PositionalOnlyContextAndVarKeyword, EXPECTED_CONTEXT),
        (_NoContext, None),
    ],
    ids=[
        "keyword_only", "var_keyword", "third_positional", "third_positional_and_var_keyword",
        "positional_only_context_and_var_keyword", "no_context",
    ],
)
def test_execute_passes_context_to_every_supported_signature(handler_cls, expected):
    handler = handler_cls({})
    state, text = _run(handler)
    assert state == TaskState.completed, text
    assert text == "ok"
    assert handler.seen == expected


# ── sync handlers ────────────────────────────────────────────────────────────
# The upload validator accepts a sync handle_structured; the executor runs it in
# a worker thread and uses its return value.

class _SyncNoContext(AgentHandler):
    def handle_structured(self, text, files=None):
        self.seen = None
        return {"answer": f"sync:{text}"}


class _SyncKeywordContext(AgentHandler):
    def handle_structured(self, text, files=None, context=None):
        self.seen = context
        return {"answer": f"sync:{text}"}


class _SyncThirdPositional(AgentHandler):
    def handle_structured(self, text, files, ctx):
        self.seen = ctx
        return {"answer": f"sync:{text}"}


@pytest.mark.parametrize(
    "handler_cls, expected",
    [
        (_SyncNoContext, None),
        (_SyncKeywordContext, EXPECTED_CONTEXT),
        (_SyncThirdPositional, EXPECTED_CONTEXT),
    ],
    ids=["no_context", "keyword_context", "third_positional"],
)
def test_execute_runs_sync_handler(handler_cls, expected):
    handler = handler_cls({})
    state, text = _run(handler)
    assert state == TaskState.completed, text
    assert text == "sync:hi"
    assert handler.seen == expected


def test_sync_handler_runs_off_the_event_loop():
    # While a sync handler blocks, the executor keeps sending `working` beats,
    # which it can only do if the handler is not running on the event loop.
    class Slow(AgentHandler):
        def handle_structured(self, text, files=None):
            time.sleep(0.5)
            return {"answer": "done"}

    state, text, events = _run_with_events(Slow({}), heartbeat_seconds=0.05)
    assert state == TaskState.completed, text
    assert text == "done"
    beats = [
        e for e in events
        if isinstance(e, TaskStatusUpdateEvent)
        and e.status.state == TaskState.working
        and e.status.message is not None
    ]
    assert len(beats) >= 3, beats


class _SyncReturnsAwaitable(AgentHandler):
    """A non-async method that returns a coroutine (e.g. a sync decorator around
    an async body) is awaited, as it was before sync handlers were supported."""

    def handle_structured(self, text, files=None):
        async def body():
            return {"answer": "awaited"}

        return body()


def test_execute_awaits_awaitable_returned_by_sync_handler():
    state, text = _run(_SyncReturnsAwaitable({}))
    assert state == TaskState.completed, text
    assert text == "awaited"


# ── results without a string answer ──────────────────────────────────────────

@pytest.mark.parametrize(
    "result, detail",
    [
        (None, "type: NoneType"),
        ("plain text", "type: str"),
        ({"text": "ok"}, "keys: ['text']"),
        ({"answer": None}, "answer of type: NoneType"),
        ({"answer": 42}, "answer of type: int"),
    ],
    ids=["none", "str", "no_answer_key", "answer_none", "answer_int"],
)
def test_execute_reports_result_without_string_answer_as_failed_task(result, detail):
    class H(AgentHandler):
        async def handle_structured(self, text, files=None):
            return result

    state, text = _run(H({}))
    assert state == TaskState.failed
    assert "must return a dict with a string 'answer'" in text
    assert detail in text


# ── handlers that can't be called or raise ───────────────────────────────────

class _ExtraRequired(AgentHandler):
    async def handle_structured(self, text, files, ctx, extra):
        return {"answer": "ok"}


class _SyncExtraRequired(AgentHandler):
    def handle_structured(self, text, files, ctx, extra):
        return {"answer": "ok"}


class _SyncRaises(AgentHandler):
    def handle_structured(self, text, files=None):
        raise RuntimeError("sync handler error")


@pytest.mark.parametrize(
    "handler_cls",
    [_ExtraRequired, _SyncExtraRequired, _SyncRaises],
    ids=["extra_required", "sync_extra_required", "sync_raises"],
)
def test_execute_reports_handler_error_as_failed_task(handler_cls):
    state, text = _run(handler_cls({}))
    assert state == TaskState.failed
    assert text.startswith("Agent failed:")
