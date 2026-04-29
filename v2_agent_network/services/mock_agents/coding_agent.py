from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Part, TextPart


class CodingAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        skill = _skill_from_context(context)
        if skill == "code/repository_inspection":
            await _repository_inspection(context, updater)
            return
        if skill == "code/bug_fix":
            await _bug_fix_plan(context, updater)
            return
        if skill == "code/pr_review":
            await _pr_review(context, updater)
            return

        await _generic_coding_output(context, updater, skill)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.reject()


async def _repository_inspection(context: RequestContext, updater: TaskUpdater) -> None:
    output = {
        "skill": "code/repository_inspection",
        "repo_summary": (
            "Mock inspection: v2_agent_network contains a PI agent, fake DirectoryClient, "
            "A2A transport adapters, and mock A2A agent servers for staged demos."
        ),
        "detected_issues": [
            "Real ADS backend remains unimplemented.",
            "State is in-memory and resets between process runs.",
            "Routing is deterministic keyword matching rather than learned intent parsing.",
        ],
        "recommended_next_task": (
            "Implement Milestone 4 real ADS publication and seventh-agent acceptance coverage."
        ),
        "risk_level": "low",
        "artifact_reference": {
            "artifact_id": "coding-repository-inspection",
            "kind": "analysis",
            "content_type": "application/json",
        },
        "context_id": context.context_id,
        "task_id": context.task_id,
    }
    await updater.start_work()
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id="coding-repository-inspection",
        name="coding_repository_inspection",
        metadata={"skill": "code/repository_inspection", "content_type": "application/json"},
    )
    await updater.complete(
        message=updater.new_agent_message(
            [Part(root=TextPart(text=output["recommended_next_task"]))],
            metadata={"structured_output": output},
        )
    )


async def _bug_fix_plan(context: RequestContext, updater: TaskUpdater) -> None:
    output = {
        "skill": "code/bug_fix",
        "confirmation_required": True,
        "proposed_action": {
            "summary": "Investigate the failing test, produce a minimal patch plan, and rerun tests.",
            "steps": [
                "Inspect the failing assertion and related implementation.",
                "Prepare the smallest code change that addresses the failure.",
                "Run the focused test and then the full v2 suite.",
            ],
            "modifies_repository": True,
        },
        "risk_level": "medium",
        "context_id": context.context_id,
        "task_id": context.task_id,
    }
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id="coding-bug-fix-plan",
        name="coding_bug_fix_plan",
        metadata={"skill": "code/bug_fix", "content_type": "application/json"},
    )
    await updater.requires_input(
        message=updater.new_agent_message(
            [Part(root=TextPart(text="Bug-fix work would modify the repository and requires confirmation."))],
            metadata={"structured_output": output},
        ),
        final=True,
    )


async def _pr_review(context: RequestContext, updater: TaskUpdater) -> None:
    output = {
        "skill": "code/pr_review",
        "review_findings": [
            {
                "severity": "medium",
                "summary": "Mock review: add regression coverage around routing and policy changes.",
            }
        ],
        "risk_level": "low",
        "context_id": context.context_id,
        "task_id": context.task_id,
    }
    await updater.start_work()
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id="coding-pr-review",
        name="coding_pr_review",
        metadata={"skill": "code/pr_review", "content_type": "application/json"},
    )
    await updater.complete(
        message=updater.new_agent_message(
            [Part(root=TextPart(text="Completed deterministic mock PR review."))],
            metadata={"structured_output": output},
        )
    )


async def _generic_coding_output(
    context: RequestContext,
    updater: TaskUpdater,
    skill: str,
) -> None:
    output = {
        "skill": skill,
        "summary": f"Prepared deterministic mock output for {skill}.",
        "risk_level": "low",
        "context_id": context.context_id,
        "task_id": context.task_id,
    }
    await updater.start_work()
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id="coding-generic-output",
        name="coding_generic_output",
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.complete(
        message=updater.new_agent_message(
            [Part(root=TextPart(text=output["summary"]))],
            metadata={"structured_output": output},
        )
    )


def _skill_from_context(context: RequestContext) -> str:
    metadata = context.metadata or {}
    return metadata.get("skill") or "code/repository_inspection"
