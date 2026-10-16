"""Startup/shutdown: DB init/close only with REGISTRATION_DB_DSN, plus the health-probe loop."""

from __future__ import annotations

import asyncio

from planner_agent.main_agent import server
from planner_agent.main_agent.config import MainAgentConfig
from planner_agent.main_agent.directory.client import DirectoryClient
from planner_agent.main_agent.llm_client import ScriptedLLMClient


def _app():
    return server.create_app(
        config=MainAgentConfig(llm_enabled=False, llm_model="mock", fake_ads=True),
        directory_client=DirectoryClient(fake_ads=True),
        llm_client=ScriptedLLMClient([]),
    )


def _probe_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if "_health_probe_loop" in t.get_coro().__qualname__]


def test_lifespan_opens_and_closes_db_when_configured(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_init() -> None:
        calls.append("init")

    async def fake_close() -> None:
        calls.append("close")

    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.setattr(server, "init_db", fake_init)
    monkeypatch.setattr(server, "close_db", fake_close)
    app = _app()

    async def go() -> None:
        async with app.router.lifespan_context(app):
            calls.append("serving")
            assert len(_probe_tasks()) == 1  # health-probe loop started
        assert _probe_tasks() == []  # and stopped on shutdown

    asyncio.run(go())
    assert calls == ["init", "serving", "close"]


def test_lifespan_skips_db_without_dsn(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)

    async def boom() -> None:
        raise AssertionError("DB must not be touched without REGISTRATION_DB_DSN")

    monkeypatch.setattr(server, "init_db", boom)
    monkeypatch.setattr(server, "close_db", boom)
    app = _app()

    async def go() -> None:
        async with app.router.lifespan_context(app):
            assert len(_probe_tasks()) == 1

    asyncio.run(go())


def test_lifespan_closes_the_llm_client_on_shutdown(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    events: list[str] = []

    class ClosingLLM(ScriptedLLMClient):
        async def aclose(self) -> None:
            events.append("llm closed")

    app = server.create_app(
        config=MainAgentConfig(llm_enabled=False, llm_model="mock", fake_ads=True),
        directory_client=DirectoryClient(fake_ads=True),
        llm_client=ClosingLLM([]),
    )

    async def go() -> None:
        async with app.router.lifespan_context(app):
            events.append("serving")

    asyncio.run(go())
    assert events == ["serving", "llm closed"]
