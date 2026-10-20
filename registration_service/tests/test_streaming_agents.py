"""Registered llm and proxy agents answer both message/send and message/stream.

The supervisor publishes every card with capabilities.streaming=True, and the
planner sends message/stream whenever the published card says so. The llm and
proxy handlers used to serve streaming=False, so the SDK rejected the planner's
call ("Streaming is not supported by the agent"). These tests spawn REAL agent
processes through SubprocessBackend — the llm agent against an in-test
OpenAI-compatible stub, the proxy against an in-test JSON-RPC upstream — and call
them with the same A2A client the planner uses.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytest.importorskip("a2a")
pytest.importorskip("uvicorn")

from a2a.types import TaskState  # noqa: E402

from agent_skeleton.container_runner import _tool_loop_card  # noqa: E402
from registration_service.backend import SubprocessBackend  # noqa: E402
from registration_service.handlers import custom_handler, llm_handler, proxy_handler  # noqa: E402
from registration_service.handlers.heartbeat import await_with_heartbeat  # noqa: E402
from registration_service.models import LLMConfig, ProxyConfig  # noqa: E402
from registration_service.supervisor import _build_agent_card  # noqa: E402
from registration_service.tests.agent_stubs import (  # noqa: E402
    a2a_call,
    free_port,
    stub_openai,
    stub_upstream,
)


_SKILLS = [{"name": "nlp/text-summarization", "id": 10101, "key": "nlp/text-summarization"}]


def test_served_cards_agree_with_published_card() -> None:
    """Every handler the subprocess backend runs, and the tool loop the cluster and
    Cloud Run images run, serves the streaming capability the supervisor publishes."""
    published = _build_agent_card("a1", "Agent", "d", _SKILLS, 19200).capabilities.streaming
    served = {
        "llm": llm_handler.build_agent_card("a1", "Agent", "d", 19200),
        "proxy": proxy_handler.build_agent_card("a1", "Agent", "d", 19200),
        "custom": custom_handler.build_agent_card("a1", "Agent", "d", 19200),
        "tool loop (container)": _tool_loop_card("a1", "Agent", "d", 19200),
    }
    for kind, card in served.items():
        assert card.capabilities.streaming == published, kind


async def _spawn(backend: SubprocessBackend, handler_type: str, config, port: int) -> str:
    agent_id = str(uuid.uuid4())
    await backend.spawn(
        agent_id=agent_id, name=f"Stream {handler_type}", description="d",
        handler_type=handler_type, config=config, port=port,
    )
    return agent_id


@pytest.mark.parametrize("stream", [False, True], ids=["send", "stream"])
async def test_llm_agent_answers(monkeypatch, stream: bool) -> None:
    with stub_openai() as (base_url, seen):
        monkeypatch.setenv("OPENAI_BASE_URL", base_url)
        monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
        # Spawned agents must import the repo by themselves (backend sets PYTHONPATH).
        monkeypatch.delenv("PYTHONPATH", raising=False)
        backend = SubprocessBackend()
        port = free_port()
        agent_id = await _spawn(
            backend, "llm", LLMConfig(system_prompt="Be brief.", model="stub-model"), port
        )
        try:
            result = await a2a_call(f"http://127.0.0.1:{port}/", "hello there", stream=stream)
        finally:
            await backend.stop(agent_id)

    assert result["card"].capabilities.streaming is True
    assert result["state"] == TaskState.completed.value
    assert result["text"] == "stub-llm says: hello there"
    assert seen and seen[0]["body"]["model"] == "stub-model"
    if stream:
        assert "TaskStatusUpdateEvent" in result["events"]


@pytest.mark.parametrize("stream", [False, True], ids=["send", "stream"])
async def test_proxy_agent_answers(monkeypatch, stream: bool) -> None:
    with stub_upstream() as (upstream_url, seen):
        monkeypatch.delenv("PYTHONPATH", raising=False)
        backend = SubprocessBackend()
        port = free_port()
        agent_id = await _spawn(backend, "proxy", ProxyConfig(upstream_url=upstream_url), port)
        try:
            result = await a2a_call(f"http://127.0.0.1:{port}/", "ping", stream=stream)
        finally:
            await backend.stop(agent_id)

    assert result["card"].capabilities.streaming is True
    assert result["state"] == TaskState.completed.value
    assert result["text"] == "upstream says: ping"
    assert len(seen) == 1


# ── Heartbeat while a slow call is in flight ─────────────────────────────────────


class _Updater:
    def __init__(self) -> None:
        self.updates: list = []

    def new_agent_message(self, parts):
        return parts

    async def update_status(self, state, message=None, final=False):
        self.updates.append(state)


async def test_heartbeat_emits_working_while_slow() -> None:
    updater = _Updater()

    async def slow() -> str:
        await asyncio.sleep(0.35)
        return "done"

    assert await await_with_heartbeat(updater, slow(), interval=0.1) == "done"
    assert len(updater.updates) >= 2
    assert set(updater.updates) == {TaskState.working}


async def test_heartbeat_silent_when_fast_and_propagates_errors() -> None:
    updater = _Updater()

    async def fast() -> int:
        return 7

    async def boom() -> None:
        raise ValueError("upstream broke")

    assert await await_with_heartbeat(updater, fast(), interval=0.1) == 7
    with pytest.raises(ValueError, match="upstream broke"):
        await await_with_heartbeat(updater, boom(), interval=0.1)
    assert updater.updates == []
