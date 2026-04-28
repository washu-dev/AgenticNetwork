from __future__ import annotations

import asyncio

import httpx

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.server import create_weather_a2a_app
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app
from v2_agent_network.services.state.store import InMemoryStateStore


WEATHER_CARD = "v2_agent_network/configs/agent_cards/weather_agent.card.json"


class SpyA2AClient(A2AClient):
    def __init__(self, wrapped: A2AClient):
        self.wrapped = wrapped
        self.calls = 0
        self.last_agent_id: str | None = None

    async def send_task(self, agent, request):
        self.calls += 1
        self.last_agent_id = agent.agent_id
        return await self.wrapped.send_task(agent, request)


def test_pi_api_fallback_discovers_weather_and_calls_via_a2a() -> None:
    asyncio.run(_pi_api_fallback_discovers_weather_and_calls_via_a2a())


async def _pi_api_fallback_discovers_weather_and_calls_via_a2a() -> None:
    card = load_agent_card(WEATHER_CARD)
    weather_app = create_weather_a2a_app(card)
    directory = DirectoryClient(fake_ads=True)
    state_store = InMemoryStateStore()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=weather_app),
        base_url=card.url,
    ) as weather_http_client:
        spy_client = SpyA2AClient(A2AClient(HTTPA2ATransport(http_client=weather_http_client)))
        pi_app = create_app(
            directory_client=directory,
            a2a_client=spy_client,
            state_store=state_store,
            publish_weather_card=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pi_app),
            base_url="http://pi-agent",
        ) as pi_http_client:
            response = await pi_http_client.post(
                "/messages",
                json={"user_id": "user-1", "channel": "api", "text": "What is the weather in Austin?"},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert spy_client.calls == 1
    assert spy_client.last_agent_id == "weather-agent"
    assert payload["selected_agent"] == "weather-agent"
    assert payload["policy_decision"] == "allowed"
    assert payload["confirmation_status"] == "not_required"
    assert payload["state"] == "completed"
    assert payload["structured_output"]["location"] == "Austin"

    invocation = state_store.get_invocation(payload["task_id"])
    context = state_store.get_context(payload["context_id"])
    assert invocation.selected_agent.agent_id == "weather-agent"
    assert invocation.policy_decision.status == "allowed"
    assert invocation.artifacts[0].artifact_id == "weather-forecast"
    assert context.user_id == "user-1"
    assert context.channel == "api"
    assert context.active_tasks == [payload["task_id"]]

