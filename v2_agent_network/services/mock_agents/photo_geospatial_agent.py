from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, FilePart, Part, TextPart


class PhotoGeospatialAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        image_uri = _image_uri_from_context(context)
        assessment = {
            "scene": "rural edge near Austin with mixed open ground and light infrastructure",
            "terrain": "mostly open terrain with a visible access route",
            "activity_indicators": ["parked vehicle shape", "linear access road", "open staging area"],
            "weather_sensitive_features": ["unpaved surface", "low drainage confidence"],
        }
        output = {
            "skill": "image/geospatial_analysis",
            "image_assessment": assessment,
            "inferred_scene_type": "peri-urban field site",
            "detected_features": [
                "access road",
                "open field",
                "possible vehicle",
                "utility corridor",
            ],
            "geospatial_notes": [
                "Treat location as approximate near Austin for this mock run.",
                "Open ground may become difficult if rain or high wind arrives.",
            ],
            "confidence": 0.82,
            "artifact_reference": {
                "uri": image_uri,
                "kind": "image",
                "content_type": "image/jpeg",
            },
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await updater.start_work()
        await updater.add_artifact(
            [Part(root=DataPart(data=output))],
            artifact_id="photo-geospatial-assessment",
            name="photo_geospatial_assessment",
            metadata={"skill": "image/geospatial_analysis", "source_uri": image_uri},
        )
        await updater.complete(
            message=updater.new_agent_message(
                [Part(root=TextPart(text="Completed deterministic geospatial photo assessment."))],
                metadata={"structured_output": output},
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.reject()


def _image_uri_from_context(context: RequestContext) -> str:
    metadata = context.metadata or {}
    if metadata.get("image_uri"):
        return metadata["image_uri"]
    if context.message:
        for part in context.message.parts:
            root = part.root
            if isinstance(root, FilePart) and getattr(root.file, "uri", None):
                return root.file.uri
    return "file://mock/geospatial-photo-austin.jpg"

