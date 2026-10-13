"""`working` heartbeats while an llm/proxy agent waits on a slow call.

These agents advertise streaming (as custom agents do), and a streaming caller's
connection is only kept alive by bytes arriving more often than its between-bytes
read timeout (planner default A2A_HTTP_TIMEOUT_SECONDS=30s). An LLM turn or an
upstream API can take longer than that, so emit a status update every
HEARTBEAT_SECONDS — the interval custom agents get from HandlerExecutor (P3).
Calls that finish before the first beat emit nothing extra.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable, TypeVar

from a2a.types import Part, TaskState, TextPart

from agent_skeleton.handler_executor import HEARTBEAT_SECONDS

T = TypeVar("T")


async def await_with_heartbeat(
    updater: Any, aw: Awaitable[T], *, interval: float = HEARTBEAT_SECONDS
) -> T:
    """Await ``aw``, emitting a `working` update every ``interval`` seconds."""
    task = asyncio.ensure_future(aw)
    elapsed = 0.0
    try:
        while True:
            # asyncio.wait does not cancel the task on timeout.
            done, _ = await asyncio.wait({task}, timeout=interval)
            if task in done:
                return task.result()
            elapsed += interval
            await updater.update_status(
                TaskState.working,
                message=updater.new_agent_message(
                    [Part(root=TextPart(text=f"working… ({int(elapsed)}s elapsed)"))]
                ),
            )
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
