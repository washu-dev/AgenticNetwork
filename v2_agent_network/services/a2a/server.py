from __future__ import annotations

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard
from starlette.applications import Starlette

from v2_agent_network.services.mock_agents.calendar_agent import CalendarAgentExecutor
from v2_agent_network.services.mock_agents.email_agent import EmailAgentExecutor
from v2_agent_network.services.mock_agents.intelligence_agent import IntelligenceAgentExecutor
from v2_agent_network.services.mock_agents.photo_geospatial_agent import PhotoGeospatialAgentExecutor
from v2_agent_network.services.mock_agents.weather_agent import WeatherAgentExecutor


def _create_a2a_app(agent_card: AgentCard, executor) -> Starlette:
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()


def create_weather_a2a_app(agent_card: AgentCard) -> Starlette:
    return _create_a2a_app(agent_card, WeatherAgentExecutor())


def create_calendar_a2a_app(agent_card: AgentCard) -> Starlette:
    return _create_a2a_app(agent_card, CalendarAgentExecutor())


def create_email_a2a_app(agent_card: AgentCard) -> Starlette:
    return _create_a2a_app(agent_card, EmailAgentExecutor())


def create_photo_geospatial_a2a_app(agent_card: AgentCard) -> Starlette:
    return _create_a2a_app(agent_card, PhotoGeospatialAgentExecutor())


def create_intelligence_a2a_app(agent_card: AgentCard) -> Starlette:
    return _create_a2a_app(agent_card, IntelligenceAgentExecutor())
