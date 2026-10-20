"""OpenAIToolLoopClient owns its AsyncOpenAI clients: one per endpoint and event loop,
reused across requests and closed by aclose(), never dropped open for the garbage
collector to close at an arbitrary moment."""

from __future__ import annotations

import asyncio
import gc
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import openai
import pytest

from chat_app.planner import llm_client
from chat_app.planner.config import PlannerConfig
from chat_app.planner.llm_client import OpenAIToolLoopClient, TurnState

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "choices": [{
        "index": 0,
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "final_answer", "arguments": json.dumps({"text": "hi"})},
            }],
        },
    }],
}


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


class _Completions(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so connection reuse is visible

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.client_address, self.headers.get("Authorization")))
        body = json.dumps(COMPLETION).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def llm_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Completions)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/v1"


def _state() -> TurnState:
    return TurnState(context_id="ctx", user_text="hi")


class _FakeClient:
    """Stands in for AsyncOpenAI: records requests, answers or raises, records close()."""

    def __init__(self, name: str, log: list[str], fail: Exception | None = None):
        self.name = name
        self.log = log
        self.fail = fail
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        assert not self.closed, f"{self.name} used after close"
        self.log.append(f"{self.name}:{kwargs['model']}")
        if self.fail is not None:
            raise self.fail
        return openai.types.chat.ChatCompletion.model_validate(COMPLETION)

    async def close(self) -> None:
        self.closed = True


def _fake_factory(monkeypatch, fail_primary: Exception | None = None):
    """Patch _make_client: base_url None is the primary, anything else the fallback."""
    made: list[_FakeClient] = []
    log: list[str] = []

    def make(*, base_url: str | None, timeout: float, api_key: str | None = None) -> _FakeClient:
        name = "fallback" if base_url else "primary"
        client = _FakeClient(name, log, fail_primary if name == "primary" else None)
        made.append(client)
        return client

    monkeypatch.setattr(llm_client, "_make_client", make)
    return made, log


def _failover_config() -> PlannerConfig:
    return PlannerConfig(
        llm_model="primary-model",
        llm_fallback_provider="openai",
        llm_fallback_base_url="http://fallback.test/v1",
        llm_fallback_model="fallback-model",
    )


def test_requests_reuse_one_client_and_its_connection(llm_server, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", _url(llm_server))
    client = OpenAIToolLoopClient(PlannerConfig(llm_model="m"))

    async def go() -> None:
        first = await client.next_tool_call(_state())
        second = await client.next_tool_call(_state())
        assert first.tool_name == second.tool_name == "final_answer"
        assert len(client._clients) == 1
        (shared,) = client._clients.values()
        await client.aclose()
        assert shared.is_closed()
        assert client._clients == {}

    asyncio.run(go())
    (first_peer, _), (second_peer, _) = llm_server.seen
    assert first_peer == second_peer  # the second request rode the first one's connection


def test_failover_uses_the_fallback_and_both_clients_are_reused(monkeypatch) -> None:
    down = openai.APIConnectionError(request=httpx.Request("POST", "http://primary.test/v1"))
    made, log = _fake_factory(monkeypatch, fail_primary=down)
    client = OpenAIToolLoopClient(_failover_config())

    async def go() -> None:
        await client.next_tool_call(_state())
        await client.next_tool_call(_state())
        await client.aclose()

    asyncio.run(go())
    # Every request tries the primary first, then fails over with the fallback's model.
    assert log == ["primary:primary-model", "fallback:fallback-model"] * 2
    assert [c.name for c in made] == ["primary", "fallback"]
    assert all(c.closed for c in made)


def test_non_availability_error_does_not_fail_over(monkeypatch) -> None:
    denied = openai.AuthenticationError(
        "bad key",
        response=httpx.Response(401, request=httpx.Request("POST", "http://primary.test/v1")),
        body=None,
    )
    made, log = _fake_factory(monkeypatch, fail_primary=denied)
    client = OpenAIToolLoopClient(_failover_config())

    async def go() -> None:
        with pytest.raises(RuntimeError, match="AuthenticationError"):
            await client.next_tool_call(_state())
        await client.aclose()

    asyncio.run(go())
    assert log == ["primary:primary-model"]
    assert [c.name for c in made] == ["primary"]


def test_a_dropped_client_never_schedules_its_own_close() -> None:
    # The SDK's default HTTP client closes itself from __del__ by scheduling aclose()
    # on whatever loop is running when it is collected; that late close is what hung
    # an unrelated A2A connect. Clients built here must be closed explicitly instead.
    async def go() -> None:
        client = llm_client._make_client(base_url="http://127.0.0.1:9/v1", timeout=5)
        before = asyncio.all_tasks()
        del client
        gc.collect()
        assert asyncio.all_tasks() == before

    asyncio.run(go())


def test_client_is_not_reused_on_another_event_loop(monkeypatch) -> None:
    made, _ = _fake_factory(monkeypatch)
    client = OpenAIToolLoopClient(PlannerConfig(llm_model="m"))

    asyncio.run(client.next_tool_call(_state()))  # this loop ends without aclose()
    asyncio.run(client.next_tool_call(_state()))

    assert len(made) == 2
    assert len(client._clients) == 1  # the closed loop's entry is forgotten
    (current,) = client._clients.values()
    assert current is made[1]


def test_endpoint_settings_are_part_of_the_cache_key(monkeypatch) -> None:
    made, _ = _fake_factory(monkeypatch)
    client = OpenAIToolLoopClient(PlannerConfig(llm_model="m"))

    async def go() -> None:
        await client.next_tool_call(_state())
        monkeypatch.setenv("OPENAI_API_KEY", "rotated-key")
        await client.next_tool_call(_state())
        await client.next_tool_call(_state())
        await client.aclose()

    asyncio.run(go())
    assert len(made) == 2
    assert all(c.closed for c in made)


def test_vertex_mints_a_token_per_request_on_one_connection(llm_server, monkeypatch) -> None:
    tokens = iter(["token-1", "token-2"])
    monkeypatch.setattr(llm_client, "_vertex_access_token", lambda: next(tokens))
    monkeypatch.setenv("OPENAI_BASE_URL", _url(llm_server))
    monkeypatch.delenv("OPENAI_API_KEY")
    client = OpenAIToolLoopClient(PlannerConfig(llm_provider="vertex", llm_model="m"))

    async def go() -> None:
        await client.next_tool_call(_state())
        await client.next_tool_call(_state())
        assert len(client._clients) == 1
        await client.aclose()

    asyncio.run(go())
    (first_peer, first_auth), (second_peer, second_auth) = llm_server.seen
    assert (first_auth, second_auth) == ("Bearer token-1", "Bearer token-2")
    assert first_peer == second_peer
