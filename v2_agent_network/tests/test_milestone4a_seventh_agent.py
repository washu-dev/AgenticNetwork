from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    DataPart,
    Part,
    TextPart,
)

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app
from v2_agent_network.services.state.store import InMemoryStateStore


LOGISTICS_PROMPT = "Plan a supply route from base to checkpoint."


class SpyA2AClient(A2AClient):
    def __init__(self, wrapped: A2AClient | None = None):
        self.wrapped = wrapped
        self.calls: list[tuple[str, str]] = []

    async def send_task(self, agent, request):
        self.calls.append((agent.agent_id, request.skill))
        if self.wrapped is None:
            raise AssertionError("requested_skill should not have invoked A2AClient")
        return await self.wrapped.send_task(agent, request)


class LogisticsAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        output = {
            "skill": "logistics/route_planning",
            "route_summary": "Mock route: base -> supply road -> checkpoint.",
            "risk_level": "low",
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await updater.start_work()
        await updater.add_artifact(
            [Part(root=DataPart(data=output))],
            artifact_id="logistics-route-plan",
            name="logistics_route_plan",
            metadata={"skill": "logistics/route_planning"},
        )
        await updater.complete(
            message=updater.new_agent_message(
                [Part(root=TextPart(text=output["route_summary"]))],
                metadata={"structured_output": output},
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.reject()


def test_requested_skill_logistics_discovers_and_invokes_over_a2a_http() -> None:
    asyncio.run(_requested_skill_logistics_discovers_and_invokes_over_a2a_http())


async def _requested_skill_logistics_discovers_and_invokes_over_a2a_http() -> None:
    card = _logistics_card()
    logistics_app = _logistics_app(card)
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(card)
    state_store = InMemoryStateStore()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=logistics_app),
        base_url=card.url,
    ) as logistics_http_client:
        transport = HTTPA2ATransport(
            http_clients_by_origin={_origin(card.url): logistics_http_client}
        )
        spy_client = SpyA2AClient(A2AClient(transport))
        pi_app = create_app(
            directory_client=directory,
            a2a_client=spy_client,
            state_store=state_store,
            publish_weather_card=False,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pi_app),
            base_url="http://pi-agent",
        ) as pi_http_client:
            response = await pi_http_client.post(
                "/messages",
                json={
                    "user_id": "user-1",
                    "channel": "api",
                    "text": LOGISTICS_PROMPT,
                    "requested_skill": "logistics/route_planning",
                },
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert spy_client.calls == [("logistics-agent", "logistics/route_planning")]
    assert payload["workflow"] == ["logistics/route_planning"]
    assert payload["selected_agents"] == [
        {
            "skill": "logistics/route_planning",
            "agent_id": "logistics-agent",
            "task_id": payload["task_ids"]["logistics/route_planning"],
        }
    ]
    assert payload["confirmation_required"] is False
    assert payload["policy_decision"] == "allowed"
    assert payload["structured_output"]["route_summary"].startswith("Mock route:")
    assert payload["artifacts"][0]["artifact_id"] == "logistics-route-plan"

    invocation = state_store.get_invocation(payload["task_ids"]["logistics/route_planning"])
    context = state_store.get_context(payload["context_id"])
    assert invocation.selected_agent.agent_id == "logistics-agent"
    assert invocation.policy_decision.status == "allowed"
    assert context.active_tasks == [payload["task_ids"]["logistics/route_planning"]]


def test_requested_skill_unknown_returns_no_capable_agent_without_invocation() -> None:
    asyncio.run(_requested_skill_unknown_returns_no_capable_agent_without_invocation())


async def _requested_skill_unknown_returns_no_capable_agent_without_invocation() -> None:
    directory = DirectoryClient(fake_ads=True)
    spy_client = SpyA2AClient()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=spy_client,
        publish_weather_card=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Use a missing capability.",
                "requested_skill": "missing/capability",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert spy_client.calls == []
    assert payload["no_capable_agent_found"] is True
    assert payload["text"] == "no capable agent found for requested_skill missing/capability"
    assert payload["selected_agents"] == []
    assert payload["task_ids"] == {}


def test_requested_skill_confirmation_required_does_not_auto_invoke() -> None:
    asyncio.run(_requested_skill_confirmation_required_does_not_auto_invoke())


async def _requested_skill_confirmation_required_does_not_auto_invoke() -> None:
    directory = DirectoryClient(fake_ads=True)
    spy_client = SpyA2AClient()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=spy_client,
        publish_weather_card=False,
        publish_calendar_email_cards=True,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Send this email.",
                "requested_skill": "email/send",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert spy_client.calls == []
    assert payload["workflow"] == ["email/send"]
    assert payload["confirmation_required"] is True
    assert payload["pending_confirmation"]["skill"] == "email/send"
    assert payload["selected_agents"] == [
        {"skill": "email/send", "agent_id": "email-agent", "task_id": None}
    ]
    assert payload["task_ids"] == {}


def test_requested_skill_rejects_raw_endpoint_urls() -> None:
    asyncio.run(_requested_skill_rejects_raw_endpoint_urls())


async def _requested_skill_rejects_raw_endpoint_urls() -> None:
    pi_app = create_app(
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=SpyA2AClient(),
        publish_weather_card=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Call this endpoint.",
                "requested_skill": "http://127.0.0.1:9101/",
            },
        )

        assert response.status_code == 400
        assert "not an endpoint URL" in response.json()["detail"]

        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Call this endpoint without a scheme.",
                "requested_skill": "127.0.0.1:9107/",
            },
        )
        assert response.status_code == 400
        assert "not an endpoint URL" in response.json()["detail"]


def test_logistics_is_not_hardcoded_in_pi_code() -> None:
    pi_paths = [
        Path("v2_agent_network/services/pi_agent/main.py"),
        Path("v2_agent_network/services/pi_agent/router.py"),
        Path("v2_agent_network/services/pi_agent/policy.py"),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in pi_paths)
    assert "logistics" not in combined
    assert "9107" not in combined


def _logistics_card() -> AgentCard:
    return AgentCard(
        name="Logistics Agent",
        description="Test-only logistics route planning A2A agent.",
        url="http://127.0.0.1:9107/",
        provider=AgentProvider(organization="Demo Logistics Organization", url=""),
        version="0.1.0",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(
            streaming=False,
            pushNotifications=False,
            stateTransitionHistory=True,
        ),
        defaultInputModes=["text/plain", "application/json"],
        defaultOutputModes=["text/plain", "application/json"],
        security=[],
        securitySchemes={},
        skills=[
            AgentSkill(
                id="logistics/route_planning",
                name="Route Planning",
                description="Plan a deterministic mock logistics route.",
                tags=["logistics/route_planning", "logistics", "route", "planning"],
                examples=[LOGISTICS_PROMPT],
                inputModes=["text/plain", "application/json"],
                outputModes=["text/plain", "application/json"],
            )
        ],
    )


def _logistics_app(card: AgentCard):
    return A2AStarletteApplication(
        agent_card=card,
        http_handler=DefaultRequestHandler(
            agent_executor=LogisticsAgentExecutor(),
            task_store=InMemoryTaskStore(),
        ),
    ).build()


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"
