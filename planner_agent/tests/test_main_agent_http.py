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


# ---------------------------------------------------------------------------
# F3 — fail-closed identity (multi-user hardening)
# ---------------------------------------------------------------------------

def _final_answer_app():
    """A minimal app whose scripted LLM replies immediately with a final answer."""
    return create_app(
        config=_config(),
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        llm_client=ScriptedLLMClient(
            [ToolCall(tool_name="final_answer", arguments={"text": "hi"})]
        ),
    )


async def _post_message(headers: dict[str, str] | None = None):
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(
            "/messages",
            json={"channel": "api", "text": "hello"},
            headers=headers or {},
        )


def test_missing_identity_fails_closed_when_db_configured(monkeypatch) -> None:
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    resp = asyncio.run(_post_message())
    assert resp.status_code == 401, resp.text


def test_missing_identity_allowed_without_db(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    resp = asyncio.run(_post_message())
    assert resp.status_code == 200, resp.text


def test_allow_anon_flag_overrides_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.setenv("PLANNER_ALLOW_ANON", "1")
    resp = asyncio.run(_post_message())
    assert resp.status_code == 200, resp.text


def test_shib_header_becomes_user_id(monkeypatch) -> None:
    # No DB configured, so credential injection is skipped; the header still sets identity.
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    asyncio.run(_shib_header_becomes_user_id())


async def _shib_header_becomes_user_id() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"channel": "api", "text": "hello"},
            headers={"mail": "Alice@WUSTL.edu"},
        )
        assert resp.status_code == 200, resp.text
        # The session must be attributed to the (lowercased) Shib email, not demo-user.
        sessions = await client.get("/sessions", params={"user_id": "alice@wustl.edu"})
        assert len(sessions.json()["sessions"]) == 1
        demo = await client.get("/sessions", params={"user_id": "demo-user"})
        assert len(demo.json()["sessions"]) == 0
