from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

import httpx

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.server import (
    create_intelligence_a2a_app,
    create_photo_geospatial_a2a_app,
    create_weather_a2a_app,
)
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app


PROMPT = "Analyze this geospatial photo and tomorrow's weather near Austin, then give me a situation summary and plan."


async def main() -> None:
    os.environ["AGENT_NETWORK_FAKE_ADS"] = "true"
    photo_card = load_agent_card("v2_agent_network/configs/agent_cards/photo_geospatial_agent.card.json")
    weather_card = load_agent_card("v2_agent_network/configs/agent_cards/weather_agent.card.json")
    intelligence_card = load_agent_card("v2_agent_network/configs/agent_cards/intelligence_agent.card.json")
    photo_app = create_photo_geospatial_a2a_app(photo_card)
    weather_app = create_weather_a2a_app(weather_card)
    intelligence_app = create_intelligence_a2a_app(intelligence_card)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=photo_app),
        base_url=photo_card.url,
    ) as photo_http_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=weather_app),
        base_url=weather_card.url,
    ) as weather_http_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=intelligence_app),
        base_url=intelligence_card.url,
    ) as intelligence_http_client:
        transport = HTTPA2ATransport(
            http_clients_by_origin={
                _origin(photo_card.url): photo_http_client,
                _origin(weather_card.url): weather_http_client,
                _origin(intelligence_card.url): intelligence_http_client,
            }
        )
        directory = DirectoryClient(fake_ads=True)
        pi_app = create_app(
            directory_client=directory,
            a2a_client=A2AClient(transport),
            publish_weather_card=True,
            publish_geo_intelligence_cards=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pi_app),
            base_url="http://pi-agent",
        ) as pi_http_client:
            response = await pi_http_client.post(
                "/messages",
                json={"user_id": "demo-user", "channel": "api", "text": PROMPT},
            )
        response.raise_for_status()
        print(response.json())


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


if __name__ == "__main__":
    asyncio.run(main())

