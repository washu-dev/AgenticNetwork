from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient

from v3_main_agent.main_agent.config import MainAgentConfig
from v3_main_agent.main_agent.llm_client import ScriptedLLMClient, ToolCall
from v3_main_agent.main_agent.server import create_app
from v3_main_agent.tests.test_main_agent_runtime import FailA2A, EMAIL_CARD


def _config() -> MainAgentConfig:
    return MainAgentConfig(llm_enabled=False, llm_model="mock", fake_ads=True)


def test_health_endpoint() -> None:
    asyncio.run(_health())


async def _health() -> None:
    app = create_app(
        config=_config(),
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        llm_client=ScriptedLLMClient([]),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_agents_endpoint_lists_published_cards() -> None:
    asyncio.run(_agents_listing())


async def _agents_listing() -> None:
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=FailA2A(),
        llm_client=ScriptedLLMClient([]),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["agents"][0]["agent_id"] == "email-agent"
    assert "email/send" in data["agents"][0]["skills"]
