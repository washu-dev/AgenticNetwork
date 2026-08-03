"""Shared test fixtures for the registration service.

Sets FAKE_DB=1 before any import so that init_db() and get_supervisor() use the
in-memory implementations instead of postgres + ADS.
"""

from __future__ import annotations

import os

# Must be set before importing the app so that module-level code that reads the
# env var (e.g. get_supervisor inside lifespan) sees it.
os.environ.setdefault("FAKE_DB", "1")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client():
    """Yield an AsyncClient wired to the FastAPI app with a fresh in-memory DB.

    ASGITransport does not trigger the FastAPI lifespan, so we initialise the
    fake DB and supervisor directly here.  Globals are reset after each test so
    state never leaks between tests.
    """
    import registration_service.db as db_mod
    import registration_service.supervisor as sup_mod
    from registration_service.db import close_db, init_db
    from registration_service.fake_db import reset_fake_db

    reset_fake_db()
    db_mod._pool = None
    sup_mod._supervisor = None

    await init_db()          # FAKE_DB=1 → uses in-memory FakePool
    sup_mod.get_supervisor()  # creates FakeSupervisor singleton

    from registration_service.server import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    await close_db()
    reset_fake_db()
    db_mod._pool = None
    sup_mod._supervisor = None


# ── Reusable request bodies ────────────────────────────────────────────────────

LLM_PAYLOAD = {
    "name": "Test LLM Agent",
    "description": "An agent for testing",
    "handler_type": "llm",
    "oasf_skills": [{"name": "nlp/text-summarization", "id": 10101}],
    "config": {
        "system_prompt": "You are a helpful assistant.",
        "model": "openai/gpt-oss-20b",
    },
}

API_PROXY_PAYLOAD = {
    "name": "Test Proxy Agent",
    "description": "Proxies requests to an upstream service",
    "handler_type": "api_proxy",
    "oasf_skills": [{"name": "tool_interaction/api_schema_understanding", "id": 1401}],
    "config": {
        "upstream_url": "http://example.com/api",
        "auth_header": "Bearer test-token",
    },
}

CUSTOM_PAYLOAD = {
    "name": "Test Custom Agent",
    "description": "A custom uploaded agent",
    "handler_type": "custom",
    "oasf_skills": [{"name": "nlp/text-summarization", "id": 10101}],
    "config": {
        "class_name": "MyAgentHandler",
        "python_version": "3.11",
        "requirements": [],
        "extra": {"greeting": "hi"},
    },
}

VALID_HANDLER_SRC = '''
from agent_skeleton import AgentHandler, FileInput

class MyAgentHandler(AgentHandler):
    async def handle_structured(self, user_input: str, files: list = []) -> dict:
        return {"answer": "ok"}
'''

INVALID_HANDLER_SRC = '''
class MyAgentHandler:  # does not extend AgentHandler
    def handle_structured(self):
        pass
'''
