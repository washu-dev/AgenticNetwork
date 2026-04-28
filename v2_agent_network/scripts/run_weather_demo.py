from __future__ import annotations

import asyncio
import os

import httpx

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.server import create_weather_a2a_app
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app


async def main() -> None:
    os.environ["AGENT_NETWORK_FAKE_ADS"] = "true"
    card = load_agent_card("v2_agent_network/configs/agent_cards/weather_agent.card.json")
    weather_app = create_weather_a2a_app(card)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=weather_app),
        base_url=card.url,
    ) as weather_http_client:
        directory = DirectoryClient(fake_ads=True)
        pi_app = create_app(
            directory_client=directory,
            a2a_client=A2AClient(HTTPA2ATransport(http_client=weather_http_client)),
            publish_weather_card=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pi_app),
            base_url="http://pi-agent",
        ) as pi_http_client:
            response = await pi_http_client.post(
                "/messages",
                json={
                    "user_id": "demo-user",
                    "channel": "api",
                    "text": "What is the weather in Austin?",
                },
            )
        response.raise_for_status()
        print(response.json())


if __name__ == "__main__":
    asyncio.run(main())

