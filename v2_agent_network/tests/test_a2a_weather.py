from __future__ import annotations

import asyncio

import httpx
from a2a.client import A2AClient as SdkA2AClient
from a2a.types import Message, MessageSendParams, Part, Role, SendMessageRequest, Task, TextPart

from v2_agent_network.services.a2a.adapters import (
    a2a_agent_card_to_discovered_agent,
    a2a_response_to_internal_task_result,
    discovered_agent_to_a2a_agent_card,
    internal_task_request_to_a2a_request,
)
from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.server import create_weather_a2a_app
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.common.models import A2AMessage, A2APart, A2ATaskRequest
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card


WEATHER_CARD = "v2_agent_network/configs/agent_cards/weather_agent.card.json"


def test_a2a_adapter_uses_installed_sdk_types() -> None:
    card = load_agent_card(WEATHER_CARD)
    discovered = a2a_agent_card_to_discovered_agent(card)
    request = A2ATaskRequest(
        task_id="requested-task",
        context_id="ctx-adapter",
        target_agent_id=discovered.agent_id,
        skill="weather/forecast",
        user_goal="weather in Austin",
        messages=[
            A2AMessage(role="user", parts=[A2APart(kind="text", text="weather in Austin")])
        ],
    )

    sdk_request = internal_task_request_to_a2a_request(request)
    assert isinstance(sdk_request, SendMessageRequest)
    assert sdk_request.params.message.context_id == "ctx-adapter"
    assert sdk_request.params.message.task_id is None
    assert sdk_request.params.message.metadata["requested_task_id"] == "requested-task"

    sdk_card = discovered_agent_to_a2a_agent_card(discovered)
    assert sdk_card.name == card.name
    assert sdk_card.url == card.url


def test_protocol_conformance_smoke_with_real_send_message_request() -> None:
    asyncio.run(_protocol_conformance_smoke())


async def _protocol_conformance_smoke() -> None:
    card = load_agent_card(WEATHER_CARD)
    app = create_weather_a2a_app(card)
    request = SendMessageRequest(
        id="jsonrpc-1",
        params=MessageSendParams(
            message=Message(
                role=Role.user,
                message_id="msg-1",
                context_id="ctx-smoke",
                parts=[Part(root=TextPart(text="weather in Austin"))],
            )
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=card.url,
    ) as http_client:
        response = await SdkA2AClient(httpx_client=http_client, agent_card=card).send_message(
            request
        )

    assert not hasattr(response.root, "error")
    task = response.root.result
    assert isinstance(task, Task)
    assert task.context_id == "ctx-smoke"
    assert task.id
    assert task.status.state.value == "completed"
    assert task.history[0].parts[0].root.text == "weather in Austin"
    assert task.status.message.context_id == "ctx-smoke"
    assert task.status.message.task_id == task.id
    assert task.status.message.parts[0].root.text.startswith("Mock forecast for Austin")
    assert task.artifacts[0].artifact_id == "weather-forecast"
    assert task.artifacts[0].parts[0].root.data["context_id"] == "ctx-smoke"
    assert task.artifacts[0].parts[0].root.data["task_id"] == task.id

    internal = a2a_response_to_internal_task_result(response)
    assert internal.context_id == "ctx-smoke"
    assert internal.task_id == task.id
    assert internal.state == "completed"
    assert internal.artifacts[0].parts[0].data["location"] == "Austin"


def test_http_a2a_client_calls_weather_server() -> None:
    asyncio.run(_http_a2a_client_calls_weather_server())


async def _http_a2a_client_calls_weather_server() -> None:
    card = load_agent_card(WEATHER_CARD)
    agent = a2a_agent_card_to_discovered_agent(card)
    app = create_weather_a2a_app(card)
    request = A2ATaskRequest(
        context_id="ctx-client",
        target_agent_id=agent.agent_id,
        skill="weather/forecast",
        user_goal="forecast for Chicago",
        messages=[
            A2AMessage(role="user", parts=[A2APart(kind="text", text="forecast for Chicago")])
        ],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=card.url,
    ) as http_client:
        client = A2AClient(HTTPA2ATransport(http_client=http_client))
        result = await client.send_task(agent, request)

    assert result.state == "completed"
    assert result.context_id == "ctx-client"
    assert result.structured_output["location"] == "Chicago"

