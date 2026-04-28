from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Part, TextPart


class CalendarAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        skill = _skill_from_context(context)
        if skill == "calendar/search_availability":
            await _search_availability(context, updater)
            return

        output = {
            "skill": skill,
            "confirmation_required": True,
            "reason": f"{skill} changes calendar state and requires user confirmation.",
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await updater.add_artifact(
            [Part(root=DataPart(data=output))],
            artifact_id="calendar-confirmation-required",
            name="calendar_confirmation_required",
            metadata={"skill": skill},
        )
        await updater.requires_input(
            message=updater.new_agent_message(
                [Part(root=TextPart(text=output["reason"]))],
                metadata={"structured_output": output},
            ),
            final=True,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.reject()


async def _search_availability(context: RequestContext, updater: TaskUpdater) -> None:
    slots = [
        {
            "label": "Option 1",
            "start": "tomorrow 1:30 PM",
            "end": "tomorrow 2:00 PM",
            "timezone": "local",
        },
        {
            "label": "Option 2",
            "start": "tomorrow 3:00 PM",
            "end": "tomorrow 3:30 PM",
            "timezone": "local",
        },
    ]
    output = {
        "skill": "calendar/search_availability",
        "query": context.get_user_input(),
        "available_slots": slots,
        "context_id": context.context_id,
        "task_id": context.task_id,
    }
    await updater.start_work()
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id="calendar-availability",
        name="calendar_availability",
        metadata={"skill": "calendar/search_availability", "content_type": "application/json"},
    )
    message = updater.new_agent_message(
        [
            Part(
                root=TextPart(
                    text=(
                        "Available tomorrow afternoon: "
                        "Option 1 at 1:30 PM and Option 2 at 3:00 PM."
                    )
                )
            )
        ],
        metadata={"structured_output": output},
    )
    await updater.complete(message=message)


def _skill_from_context(context: RequestContext) -> str:
    metadata = context.metadata or {}
    return metadata.get("skill") or "calendar/search_availability"

