from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Part, TextPart


class EmailAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        skill = _skill_from_context(context)
        if skill == "email/draft":
            await _draft_email(context, updater)
            return
        if skill == "email/send" and not _confirmed(context):
            await _send_requires_confirmation(context, updater)
            return

        output = {
            "skill": skill,
            "status": "completed",
            "summary": f"Mock {skill} completed.",
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await updater.add_artifact(
            [Part(root=DataPart(data=output))],
            artifact_id="email-result",
            name="email_result",
            metadata={"skill": skill},
        )
        await updater.complete(
            message=updater.new_agent_message(
                [Part(root=TextPart(text=output["summary"]))],
                metadata={"structured_output": output},
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.reject()


async def _draft_email(context: RequestContext, updater: TaskUpdater) -> None:
    slots = _available_slots(context)
    first = slots[0] if slots else {"start": "tomorrow 1:30 PM", "end": "tomorrow 2:00 PM"}
    second = slots[1] if len(slots) > 1 else {"start": "tomorrow 3:00 PM", "end": "tomorrow 3:30 PM"}
    draft = {
        "to": "bob@example.com",
        "recipient_name": "Bob",
        "subject": "Meeting options for tomorrow afternoon",
        "body": (
            "Hi Bob,\n\n"
            "I checked my calendar and can meet at either "
            f"{first['start']} - {first['end']} or {second['start']} - {second['end']}.\n\n"
            "Which option works better for you?\n\n"
            "Best,\nDemo User"
        ),
    }
    output = {
        "skill": "email/draft",
        "draft_email": draft,
        "used_slots": slots,
        "context_id": context.context_id,
        "task_id": context.task_id,
    }
    await updater.start_work()
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id="email-draft",
        name="email_draft",
        metadata={"skill": "email/draft", "content_type": "application/json"},
    )
    await updater.complete(
        message=updater.new_agent_message(
            [Part(root=TextPart(text="Drafted an email to Bob with two meeting options."))],
            metadata={"structured_output": output},
        )
    )


async def _send_requires_confirmation(context: RequestContext, updater: TaskUpdater) -> None:
    output = {
        "skill": "email/send",
        "confirmation_required": True,
        "reason": "Sending email is a write action and requires explicit confirmation.",
        "context_id": context.context_id,
        "task_id": context.task_id,
    }
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id="email-send-confirmation-required",
        name="email_send_confirmation_required",
        metadata={"skill": "email/send"},
    )
    await updater.requires_input(
        message=updater.new_agent_message(
            [Part(root=TextPart(text=output["reason"]))],
            metadata={"structured_output": output},
        ),
        final=True,
    )


def _skill_from_context(context: RequestContext) -> str:
    metadata = context.metadata or {}
    return metadata.get("skill") or "email/draft"


def _confirmed(context: RequestContext) -> bool:
    metadata = context.metadata or {}
    return metadata.get("confirmed") is True


def _available_slots(context: RequestContext) -> list[dict[str, str]]:
    metadata = context.metadata or {}
    slots = metadata.get("available_slots")
    if isinstance(slots, list):
        return slots
    if context.message:
        for part in context.message.parts:
            root = part.root
            if isinstance(root, DataPart):
                value = root.data.get("available_slots")
                if isinstance(value, list):
                    return value
    return []
