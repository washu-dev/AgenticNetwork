"""A2A executor that wraps any AgentHandler.

Mirrors executor.py's dual-channel response (DataPart artifact + text message
with structured_output metadata) but delegates the actual work to a user-supplied
AgentHandler.handle_structured(). Uses the a2a_runtime guard so this module — and
therefore `import agent_skeleton` — still imports when a2a-sdk is absent; serving
raises a clear error via require_a2a().
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
from typing import TYPE_CHECKING

from agent_skeleton.a2a_runtime import (
    AgentExecutorBase,
    TaskState,
    data_part,
    require_a2a,
    task_updater,
    text_part,
)
from agent_skeleton.base import AgentHandler, FileInput

if TYPE_CHECKING:
    from a2a.server.agent_execution import RequestContext
    from a2a.server.events import EventQueue

# A `working` heartbeat must arrive more often than the caller's between-bytes
# read timeout (planner default A2A_HTTP_TIMEOUT_SECONDS=30s) or a slow handler's
# call is dropped mid-flight. 20s stays comfortably under that.
HEARTBEAT_SECONDS = 20.0
DEFAULT_MAX_RUNTIME_SECONDS = 1800.0


class HandlerExecutor(AgentExecutorBase):
    """Wraps any AgentHandler in the A2A executor interface.

    Runs handle_structured() as a background task and, while it's in flight,
    emits a `working` status update every ``heartbeat_seconds`` so long-running
    handlers keep the caller's streaming connection alive (P3). Enforces a
    ``max_runtime_seconds`` ceiling: on overrun the task is cancelled and the
    task is marked failed. Fast handlers finish before the first beat, so their
    behavior is unchanged.
    """

    def __init__(
        self,
        handler: AgentHandler,
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
    ):
        require_a2a()
        self.handler = handler
        self.heartbeat_seconds = heartbeat_seconds
        self.max_runtime_seconds = max_runtime_seconds

    async def execute(self, context: "RequestContext", event_queue: "EventQueue") -> None:
        user_input = context.get_user_input() or ""
        files = _extract_files(context)

        updater = task_updater(event_queue, context.task_id, context.context_id)
        await updater.start_work()

        # P4d: per-user credentials arrive in the request params metadata (the
        # planner injects them scoped to this agent's declared+granted creds).
        # Pass a context dict only to handlers that opted in by declaring the
        # parameter — 2-arg handlers are called exactly as before. NEVER log the
        # context: it holds raw secrets.
        if _handler_accepts_context(self.handler.handle_structured):
            request_meta = getattr(context, "metadata", None) or {}
            handler_context = {
                "credentials": request_meta.get("credentials") or {},
                "user_id": request_meta.get("user_id"),
            }
            task = asyncio.ensure_future(
                self.handler.handle_structured(user_input, files, handler_context)
            )
        else:
            task = asyncio.ensure_future(self.handler.handle_structured(user_input, files))
        elapsed = 0.0
        while True:
            # asyncio.wait does NOT cancel the task on timeout — it just tells us
            # whether it finished within this beat window.
            done, _ = await asyncio.wait({task}, timeout=self.heartbeat_seconds)
            if task in done:
                break
            elapsed += self.heartbeat_seconds
            if self.max_runtime_seconds and elapsed >= self.max_runtime_seconds:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                await updater.failed(
                    message=updater.new_agent_message(
                        [text_part(
                            f"Agent exceeded its {self.max_runtime_seconds:g}s runtime limit."
                        )]
                    )
                )
                return
            await updater.update_status(
                TaskState.working,
                message=updater.new_agent_message(
                    [text_part(f"working… ({int(elapsed)}s elapsed)")]
                ),
            )

        try:
            result = task.result()
        except Exception as exc:  # handler raised — surface as a failed task
            await updater.failed(
                message=updater.new_agent_message([text_part(f"Agent failed: {exc}")])
            )
            return

        if "answer" not in result:
            await updater.failed(
                message=updater.new_agent_message(
                    [text_part(
                        "handle_structured() must return a dict with an 'answer' key. "
                        f"Got keys: {list(result.keys())}"
                    )]
                )
            )
            return

        await updater.add_artifact(
            [data_part(result)],
            name="result",
            metadata={"content_type": "application/json"},
        )
        await updater.complete(
            message=updater.new_agent_message(
                [text_part(result["answer"])],
                metadata={"structured_output": result},
            )
        )

    async def cancel(self, context: "RequestContext", event_queue: "EventQueue") -> None:
        updater = task_updater(event_queue, context.task_id, context.context_id)
        await updater.reject()


def _handler_accepts_context(fn) -> bool:
    """True if ``handle_structured`` opts into the P4d credential context.

    Accepts either a ``context`` keyword or a 3rd positional parameter beyond
    ``user_input`` and ``files`` (``fn`` is bound, so ``self`` is excluded). A
    handler that accepts ``**kwargs`` also opts in.
    """
    try:
        params = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        return False
    if "context" in params:
        return True
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    positional = [
        p for p in params.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    ]
    return len(positional) >= 3  # user_input, files, <context>


def _extract_files(context: "RequestContext") -> list[FileInput]:
    files: list[FileInput] = []
    message = getattr(context, "message", None)
    if not message:
        return files
    for part in getattr(message, "parts", None) or []:
        root = part.root
        # FilePart
        file_part = getattr(root, "file", None)
        if file_part is None:
            continue
        raw = getattr(file_part, "bytes", None) or getattr(file_part, "data", None)
        if raw is None:
            continue
        if isinstance(raw, str):
            raw = base64.b64decode(raw)
        name = getattr(file_part, "name", None)
        mime = getattr(file_part, "mimeType", None) or getattr(file_part, "mime_type", None)
        files.append(FileInput(data=raw, name=name, mime_type=mime))
    return files
