"""ZONE 5 — The A2A executor.  (COPY; rarely edit)

This is the SINGLE class the a2a-sdk calls when a request arrives. It is the
boundary between A2A and your agent. It:

  1. READS the request from `context` — you never parse raw JSON-RPC; the SDK
     hands you context.get_user_input() and context.metadata / DataParts.
  2. CALLS the engine (llm_loop.run_agent) — pure domain work, no A2A.
  3. WRITES the result back through a TaskUpdater as a structured DataPart
     artifact + a human-readable text message (the "dual channel" the planner
     reads).

Mirrors disaster_response_agent.py:747-906 and the I/O helpers at 2112-2228,
generalized and trimmed.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from .a2a_runtime import (
    AgentExecutorBase,
    TaskState,
    data_part,
    is_data_part,
    require_a2a,
    task_updater,
    text_part,
)
from .config import AGENT_NAME
from .llm_loop import run_agent

if TYPE_CHECKING:
    from a2a.server.agent_execution import RequestContext
    from a2a.server.events import EventQueue

    from .spec import AgentSpec


# --- Reading the request --------------------------------------------------

def _user_text(context: "RequestContext") -> str:
    try:
        return str(context.get_user_input())
    except Exception:
        return ""


def _payload_from_context(context: "RequestContext") -> dict[str, Any]:
    """Merge structured input from request metadata + message parts (DataParts).
    Mirrors disaster's _a2a_payload_from_context (2127-2140)."""
    payload: dict[str, Any] = dict(getattr(context, "metadata", None) or {})
    message = getattr(context, "message", None)
    if message:
        msg_meta = getattr(message, "metadata", None)
        if isinstance(msg_meta, dict):
            payload.update(msg_meta)
        for part in getattr(message, "parts", []) or []:
            root = part.root
            part_meta = getattr(root, "metadata", None) or getattr(part, "metadata", None)
            if isinstance(part_meta, dict):
                payload.update(part_meta)
            if is_data_part(root) and isinstance(getattr(root, "data", None), dict):
                payload.update(root.data)
    return payload


def _skill_from_context(context: "RequestContext") -> str | None:
    meta = getattr(context, "metadata", None)
    if isinstance(meta, dict):
        return meta.get("skill") or meta.get("requested_skill") or meta.get("skill_hint")
    return None


# --- Writing the response (dual channel: DataPart artifact + text message) -

async def _complete(updater: Any, *, text: str, output: dict[str, Any], artifact_name: str) -> None:
    await updater.start_work()
    await updater.add_artifact(
        [data_part(output)],
        name=artifact_name,
        metadata={"content_type": "application/json"},
    )
    await updater.complete(
        message=updater.new_agent_message([text_part(text)], metadata={"structured_output": output})
    )


async def _failed(updater: Any, *, text: str, output: dict[str, Any], artifact_name: str) -> None:
    await updater.start_work()
    await updater.add_artifact(
        [data_part(output)], name=artifact_name, metadata={"content_type": "application/json"}
    )
    await updater.failed(
        message=updater.new_agent_message([text_part(text)], metadata={"structured_output": output})
    )


async def _requires_input(updater: Any, *, text: str, output: dict[str, Any], artifact_name: str) -> None:
    await updater.add_artifact(
        [data_part(output)], name=artifact_name, metadata={"content_type": "application/json"}
    )
    await updater.requires_input(
        message=updater.new_agent_message([text_part(text)], metadata={"structured_output": output}),
        final=True,
    )


# --- Optional "still working" heartbeat (for slow agents) ----------------

async def _start_heartbeat(updater: Any, *, interval: float = 20.0) -> "asyncio.Task[None]":
    started = time.monotonic()

    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            msg = updater.new_agent_message(
                [text_part(f"{AGENT_NAME} is working...")],
                metadata={"progress_event": {"kind": "heartbeat", "elapsed_s": round(time.monotonic() - started, 1)}},
            )
            await updater.update_status(TaskState.working, message=msg, final=False)

    return asyncio.create_task(beat())


async def _stop_heartbeat(task: "asyncio.Task[None] | None") -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class SkeletonAgentExecutor(AgentExecutorBase):
    """A2A AgentExecutor: input via `context`, output via TaskUpdater."""

    def __init__(self, model: str | None = None, spec: "AgentSpec | None" = None):
        require_a2a()
        self.model = model
        # The AgentSpec selects this agent's prompt + tools. None => the demo
        # default (see llm_loop.run_agent), preserving the stock behavior.
        self.spec = spec

    async def execute(self, context: "RequestContext", event_queue: "EventQueue") -> None:
        updater = task_updater(event_queue, context.task_id, context.context_id)
        skill_hint = _skill_from_context(context)
        payload = _payload_from_context(context)
        user_text = _user_text(context)

        # Establish the task's input: prefer explicit fields, fall back to text.
        request_text = str(
            payload.get("prompt") or payload.get("message") or payload.get("text") or user_text or ""
        ).strip()

        if not request_text:
            await _requires_input(
                updater,
                text="Please provide a request (a 'prompt' field or message text).",
                output={
                    "status": "input_required",
                    "missing": ["prompt"],
                    "skill_hint": skill_hint,
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
                artifact_name="skeleton_input_required",
            )
            return

        heartbeat = await _start_heartbeat(updater)
        try:
            engine_payload = {"prompt": request_text, **{k: v for k, v in payload.items() if k != "prompt"}}
            # run_agent is synchronous/blocking (LLM call); keep the event loop free.
            result = await asyncio.to_thread(run_agent, engine_payload, spec=self.spec, model=self.model)
            result.update(
                {
                    "skill_hint": skill_hint,
                    "chosen_skill": skill_hint or "skeleton/answer",
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                }
            )
            await _stop_heartbeat(heartbeat)
            heartbeat = None
            await _complete(
                updater,
                text=result.get("response_text") or result.get("answer") or "",
                output=result,
                artifact_name="skeleton_result",
            )
        except Exception as exc:
            await _stop_heartbeat(heartbeat)
            heartbeat = None
            await _failed(
                updater,
                text=f"{type(exc).__name__}: {exc}",
                output={
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
                artifact_name="skeleton_error",
            )
        finally:
            await _stop_heartbeat(heartbeat)

    async def cancel(self, context: "RequestContext", event_queue: "EventQueue") -> None:
        updater = task_updater(event_queue, context.task_id, context.context_id)
        await updater.reject()
