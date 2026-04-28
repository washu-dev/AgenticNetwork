from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Part, TextPart


class IntelligenceAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        previous_outputs, source_artifacts = _workflow_context(context)
        photo = previous_outputs.get("image/geospatial_analysis", {})
        weather = previous_outputs.get("weather/forecast", {})
        output = {
            "skill": _skill_from_context(context),
            "situation_summary": (
                "Mock assessment: a peri-urban Austin field site appears accessible, "
                "but tomorrow's mild forecast still leaves surface and wind sensitivity to manage."
            ),
            "key_risks": [
                "Unpaved or open ground could slow movement if conditions change.",
                "Light wind may affect exposed setup or small equipment.",
                "Location and scene interpretation are mock estimates and need confirmation.",
            ],
            "recommended_plan": [
                "Use the visible access route and avoid staging on low-confidence drainage areas.",
                "Schedule outdoor work during the calmer part of the forecast window.",
                "Confirm site access and image location before committing resources.",
            ],
            "assumptions": [
                "The image URI is a mock reference, not a large inline image.",
                "Weather output is the deterministic mock Weather Agent forecast.",
                "No external action is executed by this intelligence summary.",
            ],
            "source_artifacts": source_artifacts,
            "consumed_inputs": {
                "photo_scene_type": photo.get("inferred_scene_type"),
                "weather_summary": weather.get("summary"),
                "source_artifact_count": len(source_artifacts),
            },
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await updater.start_work()
        await updater.add_artifact(
            [Part(root=DataPart(data=output))],
            artifact_id="intelligence-situation-summary",
            name="intelligence_situation_summary",
            metadata={"skill": output["skill"]},
        )
        await updater.complete(
            message=updater.new_agent_message(
                [Part(root=TextPart(text=output["situation_summary"]))],
                metadata={"structured_output": output},
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.reject()


def _workflow_context(context: RequestContext) -> tuple[dict[str, object], list[dict[str, str]]]:
    metadata = context.metadata or {}
    previous_outputs = metadata.get("previous_outputs")
    source_artifacts = metadata.get("source_artifacts")
    if isinstance(previous_outputs, dict) and isinstance(source_artifacts, list):
        return previous_outputs, source_artifacts
    if context.message:
        for part in context.message.parts:
            root = part.root
            if isinstance(root, DataPart):
                data = root.data
                previous = data.get("previous_outputs")
                artifacts = data.get("source_artifacts")
                if isinstance(previous, dict) and isinstance(artifacts, list):
                    return previous, artifacts
    return {}, []


def _skill_from_context(context: RequestContext) -> str:
    metadata = context.metadata or {}
    return metadata.get("skill") or "intelligence/situation_summary"

