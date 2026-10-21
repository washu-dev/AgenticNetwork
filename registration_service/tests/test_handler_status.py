"""The final task state ``llm`` and ``proxy`` agents report.

``LLMAgentExecutor`` (shared by the ``llm``, ``upload`` and ``http`` tool sources)
completes the task with the engine's answer, and ``ProxyExecutor`` with the
upstream's reply. Each reports an error (from the engine, the upstream request,
or a failure the upstream reports) as a failed task, as ``HandlerExecutor`` does
for custom agents. No network: ``run_agent`` and the upstream request are
replaced with stubs.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")

from a2a.server.agent_execution import RequestContext  # noqa: E402
from a2a.types import (  # noqa: E402
    Message,
    MessageSendParams,
    Part,
    Role,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)

from agent_skeleton.spec import llm_wrapper_spec  # noqa: E402
from registration_service.handlers import llm_handler  # noqa: E402
from registration_service.handlers.llm_handler import LLMAgentExecutor  # noqa: E402
from registration_service.handlers.proxy_handler import ProxyExecutor  # noqa: E402


class _Queue:
    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


def _context(text: str = "hello") -> RequestContext:
    message = Message(role=Role.user, message_id="m-1", parts=[Part(root=TextPart(text=text))])
    return RequestContext(
        request=MessageSendParams(message=message), task_id="task-1", context_id="ctx-1"
    )


async def _final_status(monkeypatch, run_agent):
    monkeypatch.setattr(llm_handler, "run_agent", run_agent)
    spec = llm_wrapper_spec(name="Agent", system_prompt="p", preset_tools=[], model="test-model")
    return await _final(LLMAgentExecutor("a1", spec, "test-model"))


async def _final(executor):
    queue = _Queue()
    await executor.execute(_context(), queue)
    final = [e for e in queue.events if isinstance(e, TaskStatusUpdateEvent) and e.final]
    assert len(final) == 1, queue.events
    status = final[0].status
    text = " ".join(p.root.text for p in status.message.parts) if status.message else ""
    return status.state, text


async def test_llm_executor_completes_with_engine_answer(monkeypatch) -> None:
    state, text = await _final_status(
        monkeypatch, lambda payload, **kwargs: {"answer": "hi there"}
    )
    assert state == TaskState.completed
    assert text == "hi there"


async def test_llm_executor_reports_engine_error_as_failed_task(monkeypatch) -> None:
    def boom(payload, **kwargs):
        raise RuntimeError("model unavailable")

    state, text = await _final_status(monkeypatch, boom)
    assert state == TaskState.failed
    assert text == "Agent failed: model unavailable"


async def test_proxy_executor_reports_upstream_error_as_failed_task(monkeypatch) -> None:
    async def unreachable(self, payload, headers):
        raise ConnectionError("upstream unreachable")

    monkeypatch.setattr(ProxyExecutor, "_forward", unreachable)
    state, text = await _final(ProxyExecutor("p1", "http://upstream.example.test/a2a", None))
    assert state == TaskState.failed
    assert text == "Upstream request failed: upstream unreachable"


@pytest.mark.parametrize(
    "reply, says",
    [
        ({"jsonrpc": "2.0", "id": "task-1", "error": {"code": -32603, "message": "boom"}}, "boom"),
        (
            {"jsonrpc": "2.0", "id": "task-1", "result": {"status": {
                "state": "failed",
                "message": {"parts": [{"kind": "text", "text": "Agent failed: no model"}]},
            }}},
            "Agent failed: no model",
        ),
    ],
)
async def test_proxy_executor_reports_an_upstream_failure_as_failed_task(
    monkeypatch, reply, says
) -> None:
    async def answers(self, payload, headers):
        return reply

    monkeypatch.setattr(ProxyExecutor, "_forward", answers)
    state, text = await _final(ProxyExecutor("p1", "http://upstream.example.test/a2a", None))
    assert state == TaskState.failed
    assert says in text


@pytest.mark.parametrize("extra", [{}, {"error": None}])  # some upstreams send a null error
async def test_proxy_executor_completes_with_the_upstream_answer(monkeypatch, extra) -> None:
    async def answers(self, payload, headers):
        return {"jsonrpc": "2.0", "id": "task-1", "result": {"status": {
            "state": "completed",
            "message": {"parts": [{"kind": "text", "text": "hi there"}]},
        }}, **extra}

    monkeypatch.setattr(ProxyExecutor, "_forward", answers)
    state, text = await _final(ProxyExecutor("p1", "http://upstream.example.test/a2a", None))
    assert state == TaskState.completed
    assert text == "hi there"
