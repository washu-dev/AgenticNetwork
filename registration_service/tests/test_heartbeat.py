"""P3: HandlerExecutor heartbeat + max_runtime_seconds behavior.

Drives HandlerExecutor.execute with a fake event queue + request context and a
scripted slow/fast handler, then inspects the emitted TaskStatusUpdateEvents.
Skipped when a2a-sdk isn't installed (the executor requires it to serve).
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a")

from a2a.types import TaskState, TaskStatusUpdateEvent  # noqa: E402

from agent_skeleton.base import AgentHandler  # noqa: E402
from agent_skeleton.handler_executor import HandlerExecutor  # noqa: E402

pytestmark = pytest.mark.asyncio


class _Queue:
    """Minimal EventQueue stand-in: captures whatever the TaskUpdater enqueues."""

    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


class _Ctx:
    """Minimal RequestContext: only the attributes execute() reads."""

    def __init__(self, text: str = "") -> None:
        self._text = text
        self.task_id = "task-1"
        self.context_id = "ctx-1"
        self.message = None  # -> _extract_files returns []

    def get_user_input(self) -> str:
        return self._text


class _SleepHandler(AgentHandler):
    def __init__(self, delay: float, *, answer: str = "done") -> None:
        super().__init__({})
        self.delay = delay
        self._answer = answer

    async def handle_structured(self, user_input, files=[]) -> dict:
        await asyncio.sleep(self.delay)
        return {"answer": self._answer}


def _status(events, state) -> list:
    return [
        e for e in events
        if isinstance(e, TaskStatusUpdateEvent) and e.status.state == state
    ]


async def test_fast_handler_emits_no_heartbeat() -> None:
    q = _Queue()
    ex = HandlerExecutor(_SleepHandler(0.0), heartbeat_seconds=5.0, max_runtime_seconds=30)
    await ex.execute(_Ctx("hi"), q)
    # Only start_work's `working` event — the handler finishes before the first beat.
    assert len(_status(q.events, TaskState.working)) == 1
    assert len(_status(q.events, TaskState.completed)) == 1
    assert len(_status(q.events, TaskState.failed)) == 0


async def test_slow_handler_emits_heartbeat() -> None:
    q = _Queue()
    ex = HandlerExecutor(_SleepHandler(0.35), heartbeat_seconds=0.1, max_runtime_seconds=30)
    await ex.execute(_Ctx("hi"), q)
    # start_work + at least one interim beat while awaiting.
    assert len(_status(q.events, TaskState.working)) >= 2
    assert len(_status(q.events, TaskState.completed)) == 1


async def test_max_runtime_exceeded_fails() -> None:
    q = _Queue()
    ex = HandlerExecutor(_SleepHandler(10.0), heartbeat_seconds=0.05, max_runtime_seconds=0.15)
    await ex.execute(_Ctx("hi"), q)
    assert len(_status(q.events, TaskState.failed)) == 1
    assert len(_status(q.events, TaskState.completed)) == 0


async def test_handler_exception_marks_failed() -> None:
    class _Boom(AgentHandler):
        async def handle_structured(self, user_input, files=[]) -> dict:
            raise RuntimeError("kaboom")

    q = _Queue()
    ex = HandlerExecutor(_Boom({}), heartbeat_seconds=5.0, max_runtime_seconds=30)
    await ex.execute(_Ctx("hi"), q)
    failed = _status(q.events, TaskState.failed)
    assert len(failed) == 1
    assert len(_status(q.events, TaskState.completed)) == 0
