from __future__ import annotations

import re

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Part, TaskState, TextPart


class WeatherAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        prompt = context.get_user_input()
        location = _extract_location(prompt)
        forecast = {
            "location": location,
            "summary": f"Mock forecast for {location}: mild with light wind.",
            "temperature_f": 72,
            "conditions": "partly cloudy",
            "source": "mock-weather-agent",
            "skill": "weather/forecast",
            "context_id": context.context_id,
            "task_id": context.task_id,
        }

        await updater.start_work()
        await updater.add_artifact(
            [Part(root=DataPart(data=forecast))],
            artifact_id="weather-forecast",
            name="weather_forecast",
            metadata={"skill": "weather/forecast", "content_type": "application/json"},
        )
        message = updater.new_agent_message(
            [Part(root=TextPart(text=forecast["summary"]))],
            metadata={"structured_output": forecast},
        )
        await updater.complete(message=message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.update_status(TaskState.rejected)


def _extract_location(prompt: str) -> str:
    match = re.search(r"\b(?:in|for|near)\s+([A-Za-z][A-Za-z .,-]{1,60})", prompt)
    if match:
        return match.group(1).strip(" .,-")
    return "the requested location"
