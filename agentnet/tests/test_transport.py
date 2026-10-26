"""HTTPA2ATransport against small A2A peers on a local socket: the http client's timeout
bounds streaming calls (a peer that stalls after one event, or never replies), and an error
reply to a streaming call reports its own HTTP status. None of this may depend on the
installed a2a-sdk version."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from a2a.client import A2AClientHTTPError, A2AClientTimeoutError

from agentnet.a2a.transport import HTTPA2ATransport
from agentnet.models import A2AMessage, A2APart, A2ATaskRequest, A2ATaskState, DiscoveredAgent

TIMEOUT = 1.0  # seconds: the transport's timeout in these tests
SSE_HEAD = (
    b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
    b"cache-control: no-cache\r\nconnection: close\r\n\r\n"
)
FORBIDDEN = (
    b"HTTP/1.1 403 Forbidden\r\ncontent-type: text/html\r\n"
    b"content-length: 9\r\nconnection: close\r\n\r\nForbidden"
)
REQUEST = A2ATaskRequest(
    context_id="c1",
    target_agent_id="peer",
    user_goal="hello",
    messages=[A2AMessage(role="user", parts=[A2APart(kind="text", text="hello")])],
)


def _agent(url: str, *, streaming: bool = True) -> DiscoveredAgent:
    card = {
        "name": "Peer", "description": "A test peer.", "url": url, "version": "0.1.0",
        "protocolVersion": "0.3.0", "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": streaming},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"], "skills": [],
    }
    return DiscoveredAgent(
        agent_id="peer", name="Peer", organization="Test", description="A test peer.",
        endpoint=url, source_record_id="r1", raw_record={"interaction": {"agent_card": card}},
    )


def _event(request_id: Any, result: dict[str, Any]) -> bytes:
    return f"data: {json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result})}\n\n".encode()


def _status(state: str, *, final: bool) -> dict[str, Any]:
    return {"kind": "status-update", "taskId": "t1", "contextId": "c1",
            "status": {"state": state}, "final": final}


async def _read_request(reader: asyncio.StreamReader) -> dict[str, Any]:
    head = await reader.readuntil(b"\r\n\r\n")
    length = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                  if line.lower().startswith(b"content-length:"))
    return json.loads(await reader.readexactly(length))


@contextlib.asynccontextmanager
async def _peer(mode: str) -> AsyncIterator[str]:
    """An A2A peer on a local port; yields its URL. ``mode``: "silent" accepts the connection
    and never replies; "stall" sends one `working` event, then nothing; "answer" streams a
    whole task but keeps the connection open; "forbidden" replies 403 with an HTML page."""
    release = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if mode == "forbidden":
                await _read_request(reader)
                writer.write(FORBIDDEN)
                await writer.drain()
                return
            if mode != "silent":
                request_id = (await _read_request(reader))["id"]
                writer.write(SSE_HEAD + _event(request_id, _status("working", final=False)))
                if mode == "answer":
                    writer.write(_event(request_id, {
                        "kind": "artifact-update", "taskId": "t1", "contextId": "c1",
                        "artifact": {"artifactId": "a1", "parts": [{"kind": "text", "text": "done"}]},
                    }))
                    writer.write(_event(request_id, _status("completed", final=True)))
                await writer.drain()
            await release.wait()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
    finally:
        release.set()
        server.close()
        await server.wait_closed()


def _send(
    mode: str, *, streaming: bool = True, timeout_seconds: float = TIMEOUT,
    client_timeout: float | None = None,
) -> tuple[Any, float]:
    """(result or raised exception, seconds taken) for one call to a ``mode`` peer, through
    an HTTPA2ATransport with ``timeout_seconds``, given an injected client with
    ``client_timeout`` if set. The outer bound turns a call that never ends into a test
    failure rather than a hung suite."""

    async def call(transport: HTTPA2ATransport) -> tuple[Any, float]:
        async with _peer(mode) as url:
            started = time.monotonic()
            try:
                outcome: Any = await asyncio.wait_for(
                    transport.send_task(_agent(url, streaming=streaming), REQUEST), TIMEOUT + 10
                )
            except asyncio.TimeoutError:
                pytest.fail(f"the call to a {mode} peer was still running after {TIMEOUT + 10}s")
            except Exception as exc:  # noqa: BLE001 - the tests check what was raised
                outcome = exc
            return outcome, time.monotonic() - started

    async def main() -> tuple[Any, float]:
        if client_timeout is None:
            return await call(HTTPA2ATransport(timeout_seconds=timeout_seconds))
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            return await call(HTTPA2ATransport(http_client=client, timeout_seconds=timeout_seconds))

    return asyncio.run(main())


@pytest.mark.parametrize("mode", ["silent", "stall"])
def test_streaming_call_to_a_peer_that_stops_sending_times_out(mode):
    outcome, elapsed = _send(mode)
    assert isinstance(outcome, A2AClientTimeoutError), repr(outcome)
    assert TIMEOUT * 0.9 <= elapsed < TIMEOUT + 2, elapsed


def test_streaming_call_uses_an_injected_clients_timeout():
    outcome, elapsed = _send("stall", timeout_seconds=30, client_timeout=TIMEOUT)
    assert isinstance(outcome, A2AClientTimeoutError), repr(outcome)
    assert elapsed < TIMEOUT + 2, elapsed


def test_streaming_call_returns_at_the_final_event():
    outcome, elapsed = _send("answer")
    assert outcome.state == A2ATaskState.completed, outcome
    assert [part.text for artifact in outcome.artifacts for part in artifact.parts] == ["done"]
    assert elapsed < TIMEOUT, elapsed  # it did not wait for the peer to close the stream


@pytest.mark.parametrize("streaming", [True, False])
def test_error_reply_reports_its_own_http_status(streaming):
    outcome, _ = _send("forbidden", streaming=streaming)
    assert isinstance(outcome, A2AClientHTTPError), repr(outcome)
    assert outcome.status_code == 403, outcome
    assert "403 Forbidden" in str(outcome), outcome
