from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

import httpx

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.server import create_calendar_a2a_app, create_email_a2a_app
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app


PROMPT = "Check my calendar tomorrow afternoon and draft an email to Bob with two meeting options."


async def main() -> None:
    os.environ["AGENT_NETWORK_FAKE_ADS"] = "true"
    calendar_card = load_agent_card("v2_agent_network/configs/agent_cards/calendar_agent.card.json")
    email_card = load_agent_card("v2_agent_network/configs/agent_cards/email_agent.card.json")
    calendar_app = create_calendar_a2a_app(calendar_card)
    email_app = create_email_a2a_app(email_card)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=calendar_app),
        base_url=calendar_card.url,
    ) as calendar_http_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=email_app),
        base_url=email_card.url,
    ) as email_http_client:
        transport = HTTPA2ATransport(
            http_clients_by_origin={
                _origin(calendar_card.url): calendar_http_client,
                _origin(email_card.url): email_http_client,
            }
        )
        directory = DirectoryClient(fake_ads=True)
        pi_app = create_app(
            directory_client=directory,
            a2a_client=A2AClient(transport),
            publish_weather_card=False,
            publish_calendar_email_cards=True,
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

