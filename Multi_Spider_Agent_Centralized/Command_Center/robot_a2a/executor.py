"""A2A AgentExecutor for Robot Agents.

The executor is a thin protocol adapter: it parses A2A Message content
and metadata, dispatches to the owning RobotAgentService, and returns
the result as an A2A Message.

Structured task payload convention
----------------------------------
The planner encodes task parameters as:

  Message.parts = [TextPart(text=<human-readable prompt or task_goal>)]
  Message.metadata = {
      "robot_task": {
          "task_type": "agent" | "direct_control",
          "mission_id": "...",
          ... (task-specific fields) ...
      }
  }

If ``metadata["robot_task"]`` is missing, the executor falls back to
treating the TextPart as a free-form direct-control prompt.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    ContentTypeNotSupportedError,
    InternalError,
    JSONRPCResponse,
    Message,
    Part,
    Role,
    TextPart,
)
from a2a.utils import new_task
from a2a.utils.errors import ServerError

logger = logging.getLogger(__name__)


class RobotAgentExecutor(AgentExecutor):
    """A2A executor that adapts JSON-RPC requests to the RobotAgentService."""

    def __init__(self, service: "RobotAgentService"):
        self._service = service
        logger.info(f"RobotAgentExecutor initialized for {service.robot_id}")

    @property
    def robot_id(self) -> str:
        return self._service.robot_id

    def _validate_request(self, context: RequestContext) -> Optional[JSONRPCResponse]:
        if not context or not context.message or not context.message.parts:
            return JSONRPCResponse(error=ContentTypeNotSupportedError())
        return None

    @staticmethod
    def _extract_payload(message: Message) -> dict:
        """Pull the structured robot_task dict out of Message.metadata.

        Accepts either a dict (normal case) or a JSON-encoded string
        (some clients serialise metadata values as strings).  Returns
        an empty dict if no payload is present.
        """
        if not message.metadata:
            return {}
        payload = message.metadata.get("robot_task")
        if payload is None:
            return {}
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, (bytes, str)):
            try:
                return json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        logger.debug("Received A2A message: %s", context.message)

        validation_error = self._validate_request(context)
        if validation_error:
            await event_queue.enqueue_event(validation_error)
            return

        prompt = context.get_user_input() or ""
        payload = self._extract_payload(context.message)

        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        try:
            result = await self._dispatch(prompt, payload)
            output = self._format_output(result)
            reply = Message(
                message_id=str(uuid4()),
                role=Role.agent,
                metadata={
                    "name": f"Robot Agent - {self.robot_id}",
                    "robot_id": self.robot_id,
                    "robot_task_result": result,
                },
                parts=[Part(TextPart(text=output))],
            )
            await event_queue.enqueue_event(reply)
        except Exception as e:
            logger.exception(f"[{self.robot_id}] A2A execute failed: {e}")
            raise ServerError(error=InternalError()) from e

    async def _dispatch(self, prompt: str, payload: dict) -> dict:
        task_type = payload.get("task_type")

        # Fallback: no structured payload → treat the text as a direct_control prompt.
        if not task_type:
            if not prompt:
                return {"status": "error", "error": "Empty request"}
            return await self._service.run_direct_control({"prompt": prompt})

        if task_type == "agent":
            return await self._service.run_agent_task(payload)
        if task_type == "direct_control":
            # Let explicit payload.prompt override the TextPart if both present.
            if "prompt" not in payload and prompt:
                payload = {**payload, "prompt": prompt}
            return await self._service.run_direct_control(payload)
        if task_type == "cancel":
            # Immediate "whatever you're doing, stop" command.  The A2A
            # spec defines ``tasks/cancel`` for cancelling a specific task
            # by id, which the Executor.cancel() method covers.  This
            # ``cancel`` task_type is an out-of-band broadcast cancel for
            # the legacy "stop the whole robot" semantics.
            return await self._service.cancel_current_mission()

        return {"status": "error", "error": f"Unknown task_type: {task_type}"}

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancel the current mission — halts the Pi immediately."""
        logger.info(f"[{self.robot_id}] A2A cancel received")
        result = await self._service.cancel_current_mission()
        reply = Message(
            message_id=str(uuid4()),
            role=Role.agent,
            metadata={
                "name": f"Robot Agent - {self.robot_id}",
                "robot_id": self.robot_id,
                "robot_task_result": result,
            },
            parts=[Part(TextPart(text=f"Cancelled: {result.get('status', '?')}"))],
        )
        await event_queue.enqueue_event(reply)

    @staticmethod
    def _format_output(result: dict[str, Any]) -> str:
        status = result.get("status", "?")
        agent_id = result.get("agent_id", "?")

        if "mission_result" in result:
            m = result["mission_result"]
            return (
                f"Robot {agent_id} {status}: mission={m.get('status', '?')}, "
                f"frames={m.get('frames_analyzed', 0)}, "
                f"elapsed={m.get('elapsed_s', 0)}s, "
                f"detections={len(m.get('detections', []))}"
            )

        if "execution_result" in result:
            e = result["execution_result"]
            return (
                f"Robot {agent_id} {status}: "
                f"direct_control={e.get('mission_status', '?')} - "
                f"{e.get('summary', '')[:200]}"
            )

        return f"Robot {agent_id} {status}: {json.dumps(result, default=str)[:500]}"
