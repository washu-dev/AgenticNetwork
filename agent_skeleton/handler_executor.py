"""A2A executor that wraps any AgentHandler.

Mirrors executor.py's dual-channel response (DataPart artifact + text message
with structured_output metadata) but delegates the actual work to a user-supplied
AgentHandler.handle_structured(). Uses the a2a_runtime guard so this module — and
therefore `import agent_skeleton` — still imports when a2a-sdk is absent; serving
raises a clear error via require_a2a().
"""
from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from agent_skeleton.a2a_runtime import (
    AgentExecutorBase,
    data_part,
    require_a2a,
    task_updater,
    text_part,
)
from agent_skeleton.base import AgentHandler, FileInput

if TYPE_CHECKING:
    from a2a.server.agent_execution import RequestContext
    from a2a.server.events import EventQueue


class HandlerExecutor(AgentExecutorBase):
    """Wraps any AgentHandler in the A2A executor interface."""

    def __init__(self, handler: AgentHandler):
        require_a2a()
        self.handler = handler

    async def execute(self, context: "RequestContext", event_queue: "EventQueue") -> None:
        user_input = context.get_user_input() or ""
        files = _extract_files(context)

        result = await self.handler.handle_structured(user_input, files)

        if "answer" not in result:
            raise ValueError(
                f"handle_structured() must return a dict with an 'answer' key. "
                f"Got keys: {list(result.keys())}"
            )

        updater = task_updater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
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
