from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from planner_agent.main_agent.directory.card_oasf_converter import load_agent_card
from planner_agent.main_agent.directory.client import DirectoryClient

from planner_agent.main_agent.config import MainAgentConfig
from planner_agent.main_agent.llm_client import ScriptedLLMClient, ToolCall
from planner_agent.main_agent.server import create_app
from planner_agent.tests.test_main_agent_runtime import FailA2A, EMAIL_CARD, SpyA2A, _geospatial_card


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


def test_messages_endpoint_accepts_image_upload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAIN_AGENT_UPLOAD_DIR", str(tmp_path))
    asyncio.run(_messages_accepts_image_upload())


async def _messages_accepts_image_upload() -> None:
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(_geospatial_card())
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "geospatial-agent",
                    "skill": "geospatial/analyze_image",
                    "task_input": {"request": "Analyze the uploaded image."},
                },
            ),
            ToolCall(tool_name="final_answer", arguments={"text": "Analyzed."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            data={"user_id": "u", "channel": "api", "text": "Analyze this image."},
            files={"image": ("storm.jpg", b"image-bytes", "image/jpeg")},
        )
    assert resp.status_code == 200, resp.text
    assert a2a.calls == [("geospatial-agent", "geospatial/analyze_image")]
    image_parts = [part for part in a2a.requests[0].messages[0].parts if part.kind == "image"]
    assert len(image_parts) == 1
    assert image_parts[0].bytes
