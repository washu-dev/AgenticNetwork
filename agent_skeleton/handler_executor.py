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
    ``max_runtime_seconds`` ceiling: on overrun the task is marked failed and an
    async handler is cancelled (a sync one runs in a worker thread, which cannot
    be stopped, so it runs to its end). Fast handlers finish before the first
    beat, so their behavior is unchanged.
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
        # Only handlers that opted in by declaring the parameter get the context
        # dict — 2-arg handlers are called exactly as before. NEVER log the
        # context: it holds raw secrets.
        request_meta = getattr(context, "metadata", None) or {}
        handler_context = {
            "credentials": request_meta.get("credentials") or {},
            "user_id": request_meta.get("user_id"),
        }
        args, kwargs = _call_args(
            self.handler.handle_structured, user_input, files, handler_context
        )
        task = asyncio.ensure_future(_call(self.handler.handle_structured, args, kwargs))
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

        if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
            if not isinstance(result, dict):
                got = f"type: {type(result).__name__}"
            elif "answer" not in result:
                got = f"keys: {list(result.keys())}"
            else:
                got = f"answer of type: {type(result['answer']).__name__}"
            await updater.failed(
                message=updater.new_agent_message(
                    [text_part(
                        f"handle_structured() must return a dict with a string 'answer'. Got {got}"
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


def _call_args(fn, user_input, files, handler_context) -> tuple[tuple, dict]:
    """The (args, kwargs) to call ``handle_structured`` (bound, so no ``self``) with.

    A handler opts into the P4d credential context by declaring it: a ``context``
    parameter gets it by keyword, a 3rd positional parameter beyond ``user_input``
    and ``files`` gets it positionally, and ``**kwargs`` gets ``context=``. Any
    other handler, or one whose signature can't be read, gets ``(user_input,
    files)``. Read from the signature, never by calling.
    """
    plain: tuple[tuple, dict] = ((user_input, files), {})
    try:
        params = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        return plain
    named = params.get("context")
    if named is not None and named.kind is not inspect.Parameter.POSITIONAL_ONLY:
        return (user_input, files), {"context": handler_context}
    positional = [
        p for p in params.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    ]
    if len(positional) >= 3:  # user_input, files, <context>
        return (user_input, files, handler_context), {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return (user_input, files), {"context": handler_context}
    return plain


async def _call(fn, args: tuple, kwargs: dict):
    # Calling inside the task means a call that can't bind fails the task like any
    # other handler error. An async handle_structured is awaited; a sync one (the
    # upload validator accepts both) runs in a worker thread so it can't block the
    # event loop or the heartbeat. A thread can't be interrupted, so on a runtime
    # overrun the task is marked failed while the sync body runs to its end.
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(result):  # a sync wrapper around an async body
        result = await result
    return result


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
