"""P4d — skeleton credential context seam.

A handler that declares a 3rd ``context`` param receives per-user credentials
from the request metadata; a 2-arg handler is unaffected; secrets never logged.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("a2a")

from a2a.types import TaskState, TaskStatusUpdateEvent  # noqa: E402

from agent_skeleton.base import AgentHandler  # noqa: E402
from agent_skeleton.handler_executor import (  # noqa: E402
    HandlerExecutor,
    _call_args,
)

SECRET = "sk-super-secret-value"


class _Queue:
    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


class _Ctx:
    """RequestContext stand-in with params metadata (where creds ride)."""

    def __init__(self, text: str = "", metadata: dict | None = None) -> None:
        self._text = text
        self.task_id = "task-1"
        self.context_id = "ctx-1"
        self.message = None
        self.metadata = metadata or {}

    def get_user_input(self) -> str:
        return self._text


def _completed(events):
    return [e for e in events if isinstance(e, TaskStatusUpdateEvent) and e.status.state == TaskState.completed]


# ── signature inspection ─────────────────────────────────────────────────────

def test_accepts_context_detection():
    class TwoArg(AgentHandler):
        async def handle_structured(self, user_input, files=[]): return {"answer": "x"}

    class ThreeArg(AgentHandler):
        async def handle_structured(self, user_input, files=[], context=None): return {"answer": "x"}

    class Kwargs(AgentHandler):
        async def handle_structured(self, user_input, files=[], **kw): return {"answer": "x"}

    ctx = {"credentials": {}}
    assert _call_args(TwoArg({}).handle_structured, "q", [], ctx) == (("q", []), {})
    assert _call_args(ThreeArg({}).handle_structured, "q", [], ctx) == (("q", []), {"context": ctx})
    assert _call_args(Kwargs({}).handle_structured, "q", [], ctx) == (("q", []), {"context": ctx})


# ── injection ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_three_arg_handler_receives_credentials():
    seen = {}

    class H(AgentHandler):
        async def handle_structured(self, user_input, files=[], context=None):
            seen.update(context or {})
            key = (context or {}).get("credentials", {}).get("openai_api_key", {}).get("api_key")
            return {"answer": f"got={bool(key)}"}

    ctx = _Ctx("hi", metadata={
        "credentials": {"openai_api_key": {"api_key": SECRET}},
        "user_id": "u@example.edu",
    })
    await HandlerExecutor(H({}), heartbeat_seconds=5.0, max_runtime_seconds=30).execute(ctx, _Queue())
    assert seen["credentials"]["openai_api_key"]["api_key"] == SECRET
    assert seen["user_id"] == "u@example.edu"


@pytest.mark.asyncio
async def test_two_arg_handler_still_runs_with_credentials_present():
    class H(AgentHandler):
        async def handle_structured(self, user_input, files=[]):
            return {"answer": "ok"}

    ctx = _Ctx("hi", metadata={"credentials": {"openai_api_key": {"api_key": SECRET}}})
    q = _Queue()
    # Must not raise (no unexpected-arg error) and must complete.
    await HandlerExecutor(H({}), heartbeat_seconds=5.0, max_runtime_seconds=30).execute(ctx, q)
    assert len(_completed(q.events)) == 1


@pytest.mark.asyncio
async def test_no_metadata_is_empty_context():
    seen = {}

    class H(AgentHandler):
        async def handle_structured(self, user_input, files=[], context=None):
            seen.update(context or {})
            return {"answer": "ok"}

    await HandlerExecutor(H({}), heartbeat_seconds=5.0, max_runtime_seconds=30).execute(_Ctx("hi"), _Queue())
    assert seen == {"credentials": {}, "user_id": None}


@pytest.mark.asyncio
async def test_secret_never_logged(caplog):
    class H(AgentHandler):
        async def handle_structured(self, user_input, files=[], context=None):
            return {"answer": "ok"}

    ctx = _Ctx("hi", metadata={"credentials": {"openai_api_key": {"api_key": SECRET}}})
    with caplog.at_level(logging.DEBUG):
        await HandlerExecutor(H({}), heartbeat_seconds=5.0, max_runtime_seconds=30).execute(ctx, _Queue())
    assert SECRET not in caplog.text
