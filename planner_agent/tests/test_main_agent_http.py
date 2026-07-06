from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from planner_agent.main_agent.directory.card_oasf_converter import load_agent_card
from planner_agent.main_agent.directory.client import DirectoryClient

from planner_agent.main_agent import conversation_store
from planner_agent.main_agent.config import MainAgentConfig
from planner_agent.main_agent.llm_client import ScriptedLLMClient, ToolCall
from planner_agent.main_agent.server import create_app
from planner_agent.main_agent.state.store import InMemoryStateStore
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
        # /sessions derives identity from the request (H2) — the query param is ignored.
        # Attribution is to the lowercased Shib email.
        alice = await client.get("/sessions", headers={"mail": "alice@wustl.edu"})
        assert len(alice.json()["sessions"]) == 1
        # A different identity sees none of Alice's sessions, even naming her as a param.
        bob = await client.get(
            "/sessions", params={"user_id": "alice@wustl.edu"}, headers={"mail": "bob@wustl.edu"}
        )
        assert len(bob.json()["sessions"]) == 0


# ---------------------------------------------------------------------------
# P4f — admin via ADMIN_EMAILS allowlist (SSO identity, no shared token)
# ---------------------------------------------------------------------------

def test_is_admin_helper(monkeypatch) -> None:
    from planner_agent.main_agent import auth
    monkeypatch.setenv("ADMIN_EMAILS", "Admin@WUSTL.edu, boss@wustl.edu")
    assert auth.is_admin("admin@wustl.edu")            # case-insensitive
    assert auth.is_admin("boss@wustl.edu")
    assert not auth.is_admin("nobody@wustl.edu")
    assert not auth.is_admin(None)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert not auth.is_admin("admin@wustl.edu")        # empty allowlist → no admins


async def _get(app, path, headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        return await client.get(path, headers=headers or {})


def test_admin_route_allows_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@wustl.edu")
    resp = asyncio.run(_get(_final_answer_app(), "/admin/stats", {"mail": "admin@wustl.edu"}))
    assert resp.status_code == 200, resp.text


def test_admin_route_forbids_non_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@wustl.edu")
    resp = asyncio.run(_get(_final_answer_app(), "/admin/stats", {"mail": "bob@wustl.edu"}))
    assert resp.status_code == 403, resp.text


def test_admin_empty_allowlist_forbids_everyone(monkeypatch) -> None:
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    resp = asyncio.run(_get(_final_answer_app(), "/admin/stats", {"mail": "admin@wustl.edu"}))
    assert resp.status_code == 403, resp.text


def test_admin_route_401_without_identity_when_db(monkeypatch) -> None:
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    monkeypatch.setenv("ADMIN_EMAILS", "admin@wustl.edu")
    resp = asyncio.run(_get(_final_answer_app(), "/admin/stats"))
    assert resp.status_code == 401, resp.text


def test_admin_register_proxy_blocks_non_admin(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@wustl.edu")
    asyncio.run(_admin_register_proxy_blocks_non_admin())


async def _admin_register_proxy_blocks_non_admin() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        # The proxy must reject an approver action from a non-admin BEFORE forwarding
        # to the registration service (so no upstream call is made).
        resp = await client.post(
            "/register/agents/some-id/approve", headers={"mail": "bob@wustl.edu"}
        )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# P4e — public landing at / , hard-gated chat UI at /app
# ---------------------------------------------------------------------------

def test_landing_is_public_and_links_to_app(monkeypatch) -> None:
    # Even with a DB configured (fail-closed identity), / needs no identity.
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    asyncio.run(_landing_is_public())


async def _landing_is_public() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/")
    assert resp.status_code == 200, resp.text
    assert "Sign in with WashU" in resp.text
    assert 'href="/app"' in resp.text


def test_app_hard_gated_when_db_configured(monkeypatch) -> None:
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    asyncio.run(_app_hard_gated())


async def _app_hard_gated() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        # No identity + DB configured → 401 (hard-gate).
        blocked = await client.get("/app")
        assert blocked.status_code == 401, blocked.text
        # With a Shib header → the chat UI is served.
        ok = await client.get("/app", headers={"mail": "alice@wustl.edu"})
        assert ok.status_code == 200, ok.text
        assert "Planner Agent" in ok.text


def test_app_served_in_dev_without_db(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    asyncio.run(_app_served_in_dev())


async def _app_served_in_dev() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/app")
    assert resp.status_code == 200, resp.text
    assert "Planner Agent" in resp.text


# ---------------------------------------------------------------------------
# H2 — ownership on data endpoints (multi-user hardening)
# ---------------------------------------------------------------------------

def test_conversation_endpoints_enforce_ownership(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    asyncio.run(_conversation_endpoints_enforce_ownership())


async def _conversation_endpoints_enforce_ownership() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        created = await client.post(
            "/messages", json={"channel": "api", "text": "alice secret"}, headers={"mail": "alice@wustl.edu"}
        )
        cid = created.json()["context_id"]

        # Owner can read.
        ok = await client.get(f"/conversations/{cid}", headers={"mail": "alice@wustl.edu"})
        assert ok.status_code == 200, ok.text
        assert any("alice secret" in m["text"] for m in ok.json()["messages"])

        # Non-owner is forbidden (not 404 — the conversation exists, it's just not theirs).
        forbidden = await client.get(f"/conversations/{cid}", headers={"mail": "bob@wustl.edu"})
        assert forbidden.status_code == 403, forbidden.text

        # Unknown id is a 404.
        missing = await client.get("/conversations/nope", headers={"mail": "alice@wustl.edu"})
        assert missing.status_code == 404, missing.text

        # Non-owner cannot delete; owner can.
        del_bob = await client.request(
            "DELETE", f"/conversations/{cid}", headers={"mail": "bob@wustl.edu"}
        )
        assert del_bob.status_code == 403, del_bob.text
        del_alice = await client.request(
            "DELETE", f"/conversations/{cid}", headers={"mail": "alice@wustl.edu"}
        )
        assert del_alice.status_code == 200, del_alice.text
        # Now gone.
        gone = await client.get(f"/conversations/{cid}", headers={"mail": "alice@wustl.edu"})
        assert gone.status_code == 404, gone.text


def test_persisted_context_rehydrates_and_enforces_owner(monkeypatch) -> None:
    """Simulates a conversation persisted in Postgres that is no longer in memory
    (post-restart / different process). Rehydration must restore it for the owner and
    reject a non-owner presenting the id."""
    # Simulate an active persistence layer without a real DB by stubbing the store.
    persisted = {"ctx-persist": ("alice@wustl.edu", ["user: earlier turn", "assistant: earlier reply"])}

    async def fake_owner(context_id):
        entry = persisted.get(context_id)
        return entry[0] if entry else None

    async def fake_load_history(context_id):
        entry = persisted.get(context_id)
        return list(entry[1]) if entry else None

    async def fake_save(context_id, history, owner_email):
        persisted[context_id] = (owner_email, list(history))

    monkeypatch.setattr(conversation_store, "owner", fake_owner)
    monkeypatch.setattr(conversation_store, "load_history", fake_load_history)
    monkeypatch.setattr(conversation_store, "save", fake_save)
    asyncio.run(_persisted_context_rehydrates_and_enforces_owner())


async def _persisted_context_rehydrates_and_enforces_owner() -> None:
    # A fresh store => the context exists only in the (stubbed) persistence layer.
    store = InMemoryStateStore()
    app = create_app(
        config=_config(),
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        state_store=store,
        llm_client=ScriptedLLMClient([ToolCall(tool_name="final_answer", arguments={"text": "ok"})]),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        # Non-owner presenting the persisted id is rejected after rehydration.
        bob = await client.post(
            "/messages",
            json={"channel": "api", "text": "peek", "context_id": "ctx-persist"},
            headers={"mail": "bob@wustl.edu"},
        )
        assert bob.status_code == 403, bob.text

        # Owner resumes: prior history is rehydrated, and the new turn appends to it.
        alice = await client.post(
            "/messages",
            json={"channel": "api", "text": "continue", "context_id": "ctx-persist"},
            headers={"mail": "alice@wustl.edu"},
        )
        assert alice.status_code == 200, alice.text
        ctx = store.get_context("ctx-persist")
        assert ctx.user_id == "alice@wustl.edu"
        history = "\n".join(ctx.conversation_history)
        assert "earlier turn" in history and "continue" in history
