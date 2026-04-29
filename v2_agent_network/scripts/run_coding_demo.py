from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

import httpx

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.server import create_coding_a2a_app
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app


READ_ONLY_PROMPT = "Review the current repo and suggest what coding task should be done next."
WRITE_RISK_PROMPT = "Fix the failing test in this repo."


async def main() -> None:
    os.environ["AGENT_NETWORK_FAKE_ADS"] = "true"
    coding_card = load_agent_card("v2_agent_network/configs/agent_cards/coding_agent.card.json")
    coding_app = create_coding_a2a_app(coding_card)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=coding_app),
        base_url=coding_card.url,
    ) as coding_http_client:
        transport = HTTPA2ATransport(
            http_clients_by_origin={_origin(coding_card.url): coding_http_client}
        )
        directory = DirectoryClient(fake_ads=True)
        pi_app = create_app(
            directory_client=directory,
            a2a_client=A2AClient(transport),
            publish_weather_card=False,
            publish_coding_card=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pi_app),
            base_url="http://pi-agent",
        ) as pi_http_client:
            read_only_response = await pi_http_client.post(
                "/messages",
                json={"user_id": "demo-user", "channel": "api", "text": READ_ONLY_PROMPT},
            )
            write_risk_response = await pi_http_client.post(
                "/messages",
                json={"user_id": "demo-user", "channel": "api", "text": WRITE_RISK_PROMPT},
            )
        read_only_response.raise_for_status()
        write_risk_response.raise_for_status()
        print(
            {
                "read_only_repository_inspection": read_only_response.json(),
                "write_risk_bug_fix": write_risk_response.json(),
            }
        )


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


if __name__ == "__main__":
    asyncio.run(main())
