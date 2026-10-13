"""Deterministic OpenAI-compatible mock LLM for the dev stack and the full-chain test.

Serves ``GET /v1/models`` and ``POST /v1/chat/completions`` (plain JSON, or an SSE
stream when the request sets ``"stream": true``). Each reply is computed from the
request alone, so the server is stateless: both callers resend the whole
conversation every step.

Two kinds of caller:

* The **planner** (its tools include ``list_agents``) gets a three-step script,
  chosen by counting ``role == "tool"`` messages:

  0. ``list_agents {}``
  1. ``call_agent`` on the listed agent whose name and description share the most
     words with the user's text (else the first one); ``final_answer`` instead if
     no agent is listed or listing failed.
  2. ``final_answer`` with the agent's reply text (or an explanation of its error).

* A **specialist** (anything else, e.g. a registered ``llm`` agent running
  ``agent_skeleton.llm_loop``) gets assistant text ``"[mock-llm] Echo: <latest
  user message>"``. The marker proves that the agent's own LLM loop ran.

Run: ``python3 -m devstack.mock_llm --port 8099``. ``MOCK_LLM_LOG=<file>`` appends
every request and response as one JSON line each. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODELS = ("mock-planner", "mock-specialist")
SPECIALIST_PREFIX = "[mock-llm]"
NO_AGENTS_TEXT = "No agents are available right now."
_ECHO_LIMIT = 300
_STOPWORDS = frozenset(
    "a an and are as at be but by can do for from have how i in is it me my of on or "
    "please so that the this to was what with you your".split()
)


# ── Reply logic (pure functions; unit-tested) ────────────────────────────────


def is_planner_request(body: dict[str, Any]) -> bool:
    for tool in body.get("tools") or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name") == "list_agents":
            return True
    return False


def planner_tool_call(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (tool name, arguments) for the planner's next step."""
    messages = body.get("messages") or []
    tool_messages = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    step = len(tool_messages)
    if step == 0:
        return "list_agents", {}

    observation = _json_or_none(_content_text(tool_messages[-1].get("content")))
    if step == 1:
        user_text = planner_user_text(messages)
        if not isinstance(observation, dict) or observation.get("status") != "ok":
            return "final_answer", {"text": f"I could not list agents: {_observation_error(observation)}"}
        agents = (observation.get("content") or {}).get("agents") or []
        agent = pick_agent(agents, user_text)
        if agent is None:
            return "final_answer", {"text": NO_AGENTS_TEXT}
        return "call_agent", {"agent_id": agent.get("agent_id"), "task_input": {"request": user_text}}

    return "final_answer", {"text": _final_text(observation)}


def planner_user_text(messages: list[Any]) -> str:
    """The user's text: the planner sends it as JSON in the first user message."""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            content = _content_text(message.get("content"))
            payload = _json_or_none(content)
            if isinstance(payload, dict) and isinstance(payload.get("user_text"), str):
                return payload["user_text"]
            return content
    return ""


def pick_agent(agents: list[Any], user_text: str) -> dict[str, Any] | None:
    """The agent sharing the most words with the user's text; ties keep list order."""
    candidates = [a for a in agents if isinstance(a, dict) and a.get("agent_id")]
    if not candidates:
        return None
    wanted = _words(user_text)
    best, best_score = candidates[0], 0
    for agent in candidates:
        score = len(wanted & _words(f"{agent.get('name', '')} {agent.get('description', '')}"))
        if score > best_score:
            best, best_score = agent, score
    return best


def specialist_reply(body: dict[str, Any]) -> str:
    """``[mock-llm] Echo: <latest user message>``, whitespace-collapsed and truncated."""
    text = ""
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            text = _user_message_text(_content_text(message.get("content")))
            break
    text = " ".join(text.split())
    if len(text) > _ECHO_LIMIT:
        text = text[: _ECHO_LIMIT - 3] + "..."
    return f"{SPECIALIST_PREFIX} Echo: {text}" if text else f"{SPECIALIST_PREFIX} Echo: (empty message)"


def build_completion(body: dict[str, Any]) -> dict[str, Any]:
    """The full ``chat.completion`` object answering ``body``."""
    model = str(body.get("model") or MODELS[0])
    seed = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:24]
    if is_planner_request(body):
        name, arguments = planner_tool_call(body)
        message: dict[str, Any] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{seed}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }
        finish_reason = "tool_calls"
    else:
        message = {"role": "assistant", "content": specialist_reply(body)}
        finish_reason = "stop"
    prompt_tokens = _approx_tokens(json.dumps(body.get("messages") or [], default=str))
    completion_tokens = _approx_tokens(json.dumps(message))
    return {
        "id": f"chatcmpl-mock-{seed}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason, "logprobs": None}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def stream_chunks(completion: dict[str, Any]) -> list[dict[str, Any]]:
    """The completion as ``chat.completion.chunk`` objects (role, content/tool call, finish)."""
    choice = completion["choices"][0]
    message = choice["message"]
    base = {
        "id": completion["id"],
        "object": "chat.completion.chunk",
        "created": completion["created"],
        "model": completion["model"],
    }

    def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}]}

    chunks = [chunk({"role": "assistant", "content": ""})]
    if message.get("tool_calls"):
        call = message["tool_calls"][0]
        chunks.append(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["function"]["name"], "arguments": call["function"]["arguments"]},
                        }
                    ]
                }
            )
        )
    elif message.get("content"):
        chunks.append(chunk({"content": message["content"]}))
    chunks.append(chunk({}, choice["finish_reason"]))
    final = {**base, "choices": [], "usage": completion["usage"]}
    return chunks + [final]


def models_payload() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": 0, "owned_by": "devstack"} for m in MODELS],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content parts
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content)


def _user_message_text(content: str) -> str:
    """Specialists often wrap the request in JSON (``{"prompt": ...}``); unwrap it."""
    payload = _json_or_none(content)
    if isinstance(payload, dict):
        for key in ("prompt", "request", "user_text", "text", "input", "query", "message"):
            if isinstance(payload.get(key), str):
                return payload[key]
        return json.dumps(payload, sort_keys=True)
    return content


def _final_text(observation: Any) -> str:
    if not isinstance(observation, dict):
        return "The agent call returned no readable result."
    content = observation.get("content") if isinstance(observation.get("content"), dict) else {}
    status = str(observation.get("status") or "")
    text = str(content.get("text") or "").strip()
    agent = content.get("agent_id") or "the agent"
    if status in ("completed", "ok") and text:
        return text
    if status in ("completed", "ok"):
        return f"{agent} completed but returned no text."
    structured = content.get("structured_output") if isinstance(content.get("structured_output"), dict) else {}
    detail = text or structured.get("error") or content.get("error") or json.dumps(content, default=str)[:300]
    return f"The call to {agent} did not succeed (status: {status or 'unknown'}): {detail}"


def _observation_error(observation: Any) -> str:
    if isinstance(observation, dict):
        content = observation.get("content")
        if isinstance(content, dict) and content.get("error"):
            return str(content["error"])
        return f"status {observation.get('status')!r}"
    return "unreadable observation"


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in _STOPWORDS}


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ── HTTP server ───────────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    server_version = "devstack-mock-llm/1"
    protocol_version = "HTTP/1.1"
    log_lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self._send_json(200, models_payload())
        elif path in ("/health", "/v1/health", ""):
            self._send_json(200, {"status": "ok", "models": list(MODELS)})
        else:
            self._send_json(404, {"error": {"message": f"unknown path {self.path}", "type": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": {"message": f"unknown path {self.path}", "type": "not_found"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
        except ValueError as exc:
            self._send_json(400, {"error": {"message": f"invalid JSON body: {exc}", "type": "invalid_request_error"}})
            return
        completion = build_completion(body)
        self._log({"request": body, "response": completion})
        if body.get("stream"):
            self._send_stream(completion)
        else:
            self._send_json(200, completion)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, completion: dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for item in stream_chunks(completion):
            self.wfile.write(f"data: {json.dumps(item)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _log(self, entry: dict[str, Any]) -> None:
        log_path = os.environ.get("MOCK_LLM_LOG", "").strip()
        if not log_path:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": _kind(entry["request"]), **entry}
        with self.log_lock, open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} mock-llm {fmt % args}\n")


def _kind(body: dict[str, Any]) -> str:
    return "planner" if is_planner_request(body) else "specialist"


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic OpenAI-compatible mock LLM.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port)
    print(f"mock-llm listening on http://{args.host}:{server.server_address[1]}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
