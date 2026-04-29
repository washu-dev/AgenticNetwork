from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Part, TextPart


class EmailAgentExecutor(AgentExecutor):
    """Placeholder Email/Workspace A2A executor for network and ADS development.

    This fixture intentionally has no Gmail, Calendar, Docs, or Sheets side
    effects. A real organization-owned Email Agent should run outside the
    network repo and be discovered through its Agent Card endpoint.
    """

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        skill = _skill_from_context(context)
        if skill == "email/draft":
            await _draft_email(context, updater)
            return
        if skill in _WRITE_SKILLS:
            await _write_requires_external_agent(context, updater, skill)
            return

        await _read_placeholder(context, updater, skill)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.reject()


_WRITE_SKILLS = {
    "email/send",
    "email/calendar/create_event",
    "email/docs/create",
    "email/docs/append",
    "email/docs/replace_text",
    "email/docs/create_and_email",
    "email/sheets/create",
    "email/sheets/write",
    "email/sheets/append",
}


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
        "mode": "placeholder",
        "draft_email": draft,
        "used_slots": slots,
        "note": "Placeholder executor only; no Gmail draft was created.",
        "context_id": context.context_id,
        "task_id": context.task_id,
    }
    await _complete(
        updater,
        skill="email/draft",
        artifact_id="email-draft",
        artifact_name="email_draft",
        text="Prepared a placeholder email draft.",
        output=output,
    )


async def _read_placeholder(context: RequestContext, updater: TaskUpdater, skill: str) -> None:
    output = {
        "skill": skill,
        "mode": "placeholder",
        "status": "completed",
        "summary": f"Placeholder {skill} completed without external side effects.",
        "note": (
            "This AgenticNetwork fixture is for ADS/A2A contract development only. "
            "Run the organization-owned Email Agent endpoint for real Gmail, "
            "Calendar, Docs, or Sheets behavior."
        ),
        "context_id": context.context_id,
        "task_id": context.task_id,
    }
    await _complete(
        updater,
        skill=skill,
        artifact_id=skill.replace("/", "-"),
        artifact_name=skill.replace("/", "_").replace("-", "_"),
        text=output["summary"],
        output=output,
    )


async def _write_requires_external_agent(
    context: RequestContext,
    updater: TaskUpdater,
    skill: str,
) -> None:
    output = {
        "skill": skill,
        "mode": "placeholder",
        "confirmation_required": True,
        "external_agent_required": True,
        "reason": (
            f"{skill} would change external Google Workspace state. "
            "This network fixture does not perform real write actions; call the "
            "organization-owned Email Agent endpoint discovered from its card."
        ),
        "context_id": context.context_id,
        "task_id": context.task_id,
    }
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id=skill.replace("/", "-") + "-external-agent-required",
        name=skill.replace("/", "_").replace("-", "_") + "_external_agent_required",
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.requires_input(
        message=updater.new_agent_message(
            [Part(root=TextPart(text=output["reason"]))],
            metadata={"structured_output": output},
        ),
        final=True,
    )


async def _complete(
    updater: TaskUpdater,
    *,
    skill: str,
    artifact_id: str,
    artifact_name: str,
    text: str,
    output: dict,
) -> None:
    await updater.start_work()
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id=artifact_id,
        name=artifact_name,
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.complete(
        message=updater.new_agent_message(
            [Part(root=TextPart(text=text))],
            metadata={"structured_output": output},
        )
    )


def _skill_from_context(context: RequestContext) -> str:
    metadata = context.metadata or {}
    return metadata.get("skill") or "email/draft"


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
