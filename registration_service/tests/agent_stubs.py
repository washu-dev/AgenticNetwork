"""Helpers for tests that spawn REAL agent processes.

Tiny in-thread HTTP stubs for what those agents call out to (an OpenAI-compatible
LLM, a JSON-RPC upstream for proxy agents), the env for the repo's fake ``dirctl``
(devstack/bin/dirctl), and an A2A caller built on the same SDK client the planner
uses. Not a test module itself.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
import warnings
from contextlib import aclosing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from devstack import fake_dirctl


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _serve(handle_post: Callable[[str, dict], dict], handle_get: Callable[[str], dict] | None = None) -> Iterator[str]:
    """Serve JSON handlers on 127.0.0.1:<free port> in a thread; yield the base URL."""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, body: dict, status: int = 200) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            self._reply(handle_post(self.path, body))

        def do_GET(self) -> None:  # noqa: N802
            if handle_get is None:
                self._reply({"error": "not found"}, 404)
            else:
                self._reply(handle_get(self.path))

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def stub_openai() -> Iterator[tuple[str, list[dict]]]:
    """OpenAI-compatible chat completions stub. Yields (base_url ending /v1, requests).

    Replies ``stub-llm says: <prompt>``; the llm handler sends the user text as
    the JSON payload {"prompt": ...}.
    """
    seen: list[dict] = []

    def post(path: str, body: dict) -> dict:
        seen.append({"path": path, "body": body})
        content = body["messages"][-1]["content"]
        try:
            prompt = json.loads(content).get("prompt", content)
        except (ValueError, AttributeError):
            prompt = content
        return {
            "id": f"cmpl-{len(seen)}",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "stub"),
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": f"stub-llm says: {prompt}"},
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def get(path: str) -> dict:
        return {"object": "list", "data": [{"id": "stub-model", "object": "model"}]}

    with _serve(post, get) as base:
        yield f"{base}/v1", seen


@contextmanager
def stub_upstream() -> Iterator[tuple[str, list[dict]]]:
    """JSON-RPC upstream for a proxy agent. Replies ``upstream says: <text>``."""
    seen: list[dict] = []

    def post(path: str, body: dict) -> dict:
        seen.append(body)
        text = body["params"]["message"]["parts"][0]["text"]
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {
                "kind": "message",
                "role": "agent",
                "messageId": uuid.uuid4().hex,
                "parts": [{"kind": "text", "text": f"upstream says: {text}"}],
            },
        }

    with _serve(post) as base:
        yield f"{base}/", seen


DIRCTL_SHIM = Path(__file__).resolve().parents[2] / "devstack" / "bin" / "dirctl"


def fake_dirctl_env(directory: Path) -> tuple[dict[str, str], Path]:
    """Env pointing the publisher at the repo's fake dirctl; returns (env, state file)."""
    state = directory / "directory.json"
    return {"DIRCTL_BIN": str(DIRCTL_SHIM), "FAKE_DIRCTL_STATE": str(state)}, state


def routed_cids(state: Path) -> list[str]:
    with fake_dirctl.locked_state(str(state), write=False) as s:
        return [c for c, e in s["records"].items() if fake_dirctl._is_routed(e, time.time())]


def _parts_text(parts: Any) -> str:
    texts = []
    for p in parts or []:
        root = getattr(p, "root", p)
        if getattr(root, "text", None):
            texts.append(root.text)
    return " ".join(texts)


async def a2a_call(base_url: str, text: str, *, stream: bool) -> dict[str, Any]:
    """Call an agent the way the planner does (a2a A2AClient, card from the agent).

    Returns {"card": AgentCard, "state": final task state, "text": final message
    text, "events": streamed event kinds (stream only)}.
    """
    from a2a.client import A2AClient
    from a2a.types import (
        AgentCard,
        Message,
        MessageSendParams,
        Part,
        Role,
        SendMessageRequest,
        SendStreamingMessageRequest,
        Task,
        TaskStatusUpdateEvent,
        TextPart,
    )

    base = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0) as http:
        card = AgentCard.model_validate(
            (await http.get(f"{base}/.well-known/agent-card.json")).json()
        )
        with warnings.catch_warnings():
            # Deprecated in the SDK, but it is what the planner's transport uses.
            warnings.simplefilter("ignore", DeprecationWarning)
            client = A2AClient(httpx_client=http, agent_card=card)
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text=text))],
                message_id=uuid.uuid4().hex,
            )
        )
        if not stream:
            resp = await client.send_message(SendMessageRequest(id=uuid.uuid4().hex, params=params))
            result = getattr(resp.root, "result", None)
            if result is None:
                raise AssertionError(f"message/send error: {resp.root.model_dump()}")
            if isinstance(result, Task):
                msg = result.status.message
                return {"card": card, "state": result.status.state.value,
                        "text": _parts_text(msg.parts if msg else []), "events": []}
            return {"card": card, "state": None, "text": _parts_text(result.parts), "events": []}

        events: list[str] = []
        state, final_text = None, ""
        req = SendStreamingMessageRequest(id=uuid.uuid4().hex, params=params)
        # aclosing: leaving at the final event (as the planner does) must close
        # the SSE stream now, inside this client's lifetime, not at GC time.
        async with aclosing(client.send_message_streaming(req)) as stream_events:
            async for envelope in stream_events:
                event = getattr(envelope.root, "result", None)
                if event is None:
                    raise AssertionError(f"message/stream error: {envelope.root.model_dump()}")
                events.append(type(event).__name__)
                if isinstance(event, TaskStatusUpdateEvent):
                    state = event.status.state.value
                    if event.status.message is not None:
                        final_text = _parts_text(event.status.message.parts) or final_text
                    if event.final:
                        break
        return {"card": card, "state": state, "text": final_text, "events": events}
