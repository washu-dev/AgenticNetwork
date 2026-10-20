from __future__ import annotations

import asyncio
import contextlib
import uuid

import httpx
import pytest

from agentnet.directory.card_oasf import load_agent_card
from agentnet.directory.client import DirectoryClient

from planner_agent.main_agent import conversation_store
from planner_agent.main_agent.config import MainAgentConfig
from planner_agent.main_agent.llm_client import ScriptedLLMClient, ToolCall
from planner_agent.main_agent.models import Channel, PIContext
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
    # Uploaded images are carried as A2A "file" parts (image/jpeg), matching
    # test_main_agent_runtime.py's image-upload assertion.
    file_parts = [part for part in a2a.requests[0].messages[0].parts if part.kind == "file"]
    assert len(file_parts) == 1
    assert file_parts[0].content_type == "image/jpeg"
    assert file_parts[0].bytes


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
            headers={"mail": "Alice@EXAMPLE.com"},
        )
        assert resp.status_code == 200, resp.text
        # /sessions derives identity from the request (H2) — the query param is ignored.
        # Attribution is to the lowercased Shib email.
        alice = await client.get("/sessions", headers={"mail": "alice@example.com"})
        assert len(alice.json()["sessions"]) == 1
        # A different identity sees none of Alice's sessions, even naming her as a param.
        bob = await client.get(
            "/sessions", params={"user_id": "alice@example.com"}, headers={"mail": "bob@example.com"}
        )
        assert len(bob.json()["sessions"]) == 0


# ---------------------------------------------------------------------------
# P4f — admin via ADMIN_EMAILS allowlist (SSO identity, no shared token)
# ---------------------------------------------------------------------------

def test_is_admin_helper(monkeypatch) -> None:
    from planner_agent.main_agent import auth
    monkeypatch.setenv("ADMIN_EMAILS", "Admin@EXAMPLE.com, boss@example.com")
    assert auth.is_admin("admin@example.com")            # case-insensitive
    assert auth.is_admin("boss@example.com")
    assert not auth.is_admin("nobody@example.com")
    assert not auth.is_admin(None)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert not auth.is_admin("admin@example.com")        # empty allowlist → no admins


async def _get(app, path, headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        return await client.get(path, headers=headers or {})


def test_admin_route_allows_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    resp = asyncio.run(_get(_final_answer_app(), "/admin/stats", {"mail": "admin@example.com"}))
    assert resp.status_code == 200, resp.text


def test_admin_route_forbids_non_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    resp = asyncio.run(_get(_final_answer_app(), "/admin/stats", {"mail": "bob@example.com"}))
    assert resp.status_code == 403, resp.text


def test_admin_empty_allowlist_forbids_everyone(monkeypatch) -> None:
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    resp = asyncio.run(_get(_final_answer_app(), "/admin/stats", {"mail": "admin@example.com"}))
    assert resp.status_code == 403, resp.text


def test_admin_route_401_without_identity_when_db(monkeypatch) -> None:
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    resp = asyncio.run(_get(_final_answer_app(), "/admin/stats"))
    assert resp.status_code == 401, resp.text


def test_admin_register_proxy_blocks_non_admin(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    asyncio.run(_admin_register_proxy_blocks_non_admin())


async def _admin_register_proxy_blocks_non_admin() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        # The proxy must reject an approver action from a non-admin BEFORE forwarding
        # to the registration service (so no upstream call is made).
        resp = await client.post(
            "/register/agents/some-id/approve", headers={"mail": "bob@example.com"}
        )
    assert resp.status_code == 403, resp.text


def test_register_proxy_blocks_non_admin_handler_review(monkeypatch) -> None:
    # GET /agents/{id}/handler is the approver's source-review action — admin-only.
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    asyncio.run(_register_proxy_blocks_non_admin_handler_review())


async def _register_proxy_blocks_non_admin_handler_review() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get(
            "/register/agents/some-id/handler", headers={"mail": "bob@example.com"}
        )
    assert resp.status_code == 403, resp.text


def test_register_proxy_allows_non_admin_handler_upload(monkeypatch) -> None:
    # POST /agents/{id}/handler is the (open) code-upload step — a non-admin must
    # NOT be blocked at the gate. It shares its path with the admin-only GET review,
    # so a method-agnostic suffix match wrongly returned 403 here (the reported bug).
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    # Dead upstream: the proxy forwarding the request (rather than rejecting it)
    # is what proves the request cleared the admin gate.
    monkeypatch.setenv("REGISTRATION_SERVICE_URL", "http://127.0.0.1:9")
    asyncio.run(_register_proxy_allows_non_admin_handler_upload())


async def _register_proxy_allows_non_admin_handler_upload() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        with pytest.raises(httpx.TransportError):
            await client.post(
                "/register/agents/some-id/handler",
                headers={"mail": "bob@example.com"},
                content=b"code",
            )


def test_register_proxy_never_forwards_internal_routes(monkeypatch) -> None:
    # Registration's /internal/* (host spawn loop) is unauthenticated; the public
    # proxy must not reach it. A dead upstream proves nothing was forwarded.
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setenv("REGISTRATION_SERVICE_URL", "http://127.0.0.1:9")
    asyncio.run(_register_proxy_never_forwards_internal_routes())


async def _register_proxy_never_forwards_internal_routes() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        for method, path in [
            ("GET", "/register/internal/spawn-queue"),
            ("POST", "/register/internal/spawned"),
            ("GET", "/register//internal/stop-queue"),
            ("GET", "/register/agents/%2e%2e/internal/stop-queue"),
            ("GET", "/register/%2569nternal/spawn-queue"),
            ("GET", "/register/agents/..%252finternal/stop-queue"),
            ("POST", "/register/internal/spawned%3F/%2e%2e/%2e%2e/agents"),
            ("POST", "/register/internal//%2e%2e/spawned"),
            ("POST", "/register//agents/internal/spawned"),
        ]:
            resp = await client.request(method, path, headers={"mail": "admin@example.com"})
            assert resp.status_code == 404, (path, resp.text)


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
        ok = await client.get("/app", headers={"mail": "alice@example.com"})
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
            "/messages", json={"channel": "api", "text": "alice secret"}, headers={"mail": "alice@example.com"}
        )
        cid = created.json()["context_id"]

        # Owner can read.
        ok = await client.get(f"/conversations/{cid}", headers={"mail": "alice@example.com"})
        assert ok.status_code == 200, ok.text
        assert any("alice secret" in m["text"] for m in ok.json()["messages"])

        # Non-owner is forbidden (not 404 — the conversation exists, it's just not theirs).
        forbidden = await client.get(f"/conversations/{cid}", headers={"mail": "bob@example.com"})
        assert forbidden.status_code == 403, forbidden.text

        # Unknown id is a 404.
        missing = await client.get("/conversations/nope", headers={"mail": "alice@example.com"})
        assert missing.status_code == 404, missing.text

        # Non-owner cannot delete; owner can.
        del_bob = await client.request(
            "DELETE", f"/conversations/{cid}", headers={"mail": "bob@example.com"}
        )
        assert del_bob.status_code == 403, del_bob.text
        del_alice = await client.request(
            "DELETE", f"/conversations/{cid}", headers={"mail": "alice@example.com"}
        )
        assert del_alice.status_code == 200, del_alice.text
        # Now gone.
        gone = await client.get(f"/conversations/{cid}", headers={"mail": "alice@example.com"})
        assert gone.status_code == 404, gone.text


def test_persisted_context_rehydrates_and_enforces_owner(monkeypatch) -> None:
    """Simulates a conversation persisted in Postgres that is no longer in memory
    (post-restart / different process). Rehydration must restore it for the owner and
    reject a non-owner presenting the id."""
    # Simulate an active persistence layer without a real DB by stubbing the store.
    persisted = {"ctx-persist": ("alice@example.com", ["user: earlier turn", "assistant: earlier reply"])}

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
            headers={"mail": "bob@example.com"},
        )
        assert bob.status_code == 403, bob.text

        # Owner resumes: prior history is rehydrated, and the new turn appends to it.
        alice = await client.post(
            "/messages",
            json={"channel": "api", "text": "continue", "context_id": "ctx-persist"},
            headers={"mail": "alice@example.com"},
        )
        assert alice.status_code == 200, alice.text
        ctx = store.get_context("ctx-persist")
        assert ctx.user_id == "alice@example.com"
        history = "\n".join(ctx.conversation_history)
        assert "earlier turn" in history and "continue" in history


# ---------------------------------------------------------------------------
# Identity — the authenticated header wins over a request body's user_id
# ---------------------------------------------------------------------------

def test_header_identity_wins_over_body_user_id(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    asyncio.run(_header_identity_wins_over_body_user_id())


async def _header_identity_wins_over_body_user_id() -> None:
    store = InMemoryStateStore()
    app = create_app(
        config=_config(),
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        state_store=store,
        llm_client=ScriptedLLMClient([ToolCall(tool_name="final_answer", arguments={"text": "hi"})]),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        created = await client.post(
            "/messages",
            json={"user_id": "someone-else", "channel": "api", "text": "hello"},
            headers={"mail": "alice@example.com"},
        )
        assert created.status_code == 200, created.text
        cid = created.json()["context_id"]
        # The new context belongs to the header identity, not the body's user_id.
        assert store.get_context(cid).user_id == "alice@example.com"
        alice = await client.get(f"/conversations/{cid}", headers={"mail": "alice@example.com"})
        assert alice.status_code == 200, alice.text
        named = await client.get(f"/conversations/{cid}", headers={"mail": "someone-else"})
        assert named.status_code == 403, named.text
        sessions = await client.get("/sessions", headers={"mail": "someone-else"})
        assert sessions.json()["sessions"] == []


def test_body_user_id_cannot_enter_another_users_context(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    asyncio.run(_body_user_id_cannot_enter_another_users_context())


async def _body_user_id_cannot_enter_another_users_context() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        created = await client.post(
            "/messages", json={"channel": "api", "text": "alice secret"}, headers={"mail": "alice@example.com"}
        )
        cid = created.json()["context_id"]
        # Bob names Alice in the body to append to her conversation: still Bob, still 403.
        bob = await client.post(
            "/messages",
            json={"user_id": "alice@example.com", "channel": "api", "text": "peek", "context_id": cid},
            headers={"mail": "bob@example.com"},
        )
        assert bob.status_code == 403, bob.text


def test_body_user_id_kept_without_identity_header(monkeypatch) -> None:
    # Anonymous dev/no-DB runs keep the body's user_id, as before.
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    asyncio.run(_body_user_id_kept_without_identity_header())


async def _body_user_id_kept_without_identity_header() -> None:
    store = InMemoryStateStore()
    app = create_app(
        config=_config(),
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        state_store=store,
        llm_client=ScriptedLLMClient([ToolCall(tool_name="final_answer", arguments={"text": "hi"})]),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/messages", json={"user_id": "u", "channel": "api", "text": "hello"})
    assert resp.status_code == 200, resp.text
    assert store.get_context(resp.json()["context_id"]).user_id == "u"


def test_confirmations_fail_closed_without_identity_when_db(monkeypatch) -> None:
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    asyncio.run(_confirmations_fail_closed())


async def _confirmations_fail_closed() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/confirmations",
            json={"user_id": "alice@example.com", "channel": "api", "context_id": "c1", "confirmed": False},
        )
    assert resp.status_code == 401, resp.text


def test_confirmations_forbid_non_owner(monkeypatch) -> None:
    # Bob cannot confirm or cancel Alice's pending action by naming her context_id.
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    asyncio.run(_confirmations_forbid_non_owner())


async def _confirmations_forbid_non_owner() -> None:
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    store = InMemoryStateStore()
    store.save_context(
        PIContext(context_id="c-alice", user_id="alice@example.com", channel=Channel.api, current_goal="send it")
    )
    pending = {
        "skill": "email/send",
        "task_input": {},
        "selected_agent": directory.search_agents()[0].model_dump(mode="json"),
    }
    store.save_pending_confirmation("c-alice", pending)
    a2a = SpyA2A()
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        state_store=store,
        llm_client=ScriptedLLMClient([]),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        for confirmed in (True, False):
            resp = await client.post(
                "/confirmations",
                json={"user_id": "alice@example.com", "context_id": "c-alice", "confirmed": confirmed},
                headers={"mail": "bob@example.com"},
            )
            assert resp.status_code == 403, resp.text
        assert a2a.calls == []
        assert store.get_pending_confirmation("c-alice") is pending
        assert store.get_context("c-alice").conversation_history == []
        # The owner can still confirm.
        resp = await client.post(
            "/confirmations",
            json={"context_id": "c-alice", "confirmed": True},
            headers={"mail": "alice@example.com"},
        )
    assert resp.status_code == 200, resp.text
    assert a2a.calls == [("email-agent", "email/send")]


# ---------------------------------------------------------------------------
# GET /me — creates the user on first call, with or without a DB
# ---------------------------------------------------------------------------

class _FakeUsersConn:
    def __init__(self, users: dict[str, dict]):
        self.users = users

    async def fetchrow(self, query: str, email: str):
        if query.startswith("SELECT"):
            return self.users.get(email)
        self.users[email] = {"id": uuid.uuid4(), "email": email}
        return self.users[email]


class _FakeUsersPool:
    """The slice of an asyncpg pool that get_or_create_user uses."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _FakeUsersConn(self.users)


def test_me_without_db_creates_in_memory_user(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    asyncio.run(_me_without_db())


async def _me_without_db() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = await client.get("/me", headers={"mail": "Carol@EXAMPLE.com"})
        again = await client.get("/me", headers={"mail": "carol@example.com"})
        other = await client.get("/me", headers={"mail": "dave@example.com"})
        admin = await client.get("/me", headers={"mail": "admin@example.com"})
    assert first.status_code == 200, first.text
    me = first.json()
    assert set(me) == {"id", "email", "is_admin"}
    assert me["email"] == "carol@example.com"
    assert me["is_admin"] is False
    assert str(uuid.UUID(me["id"])) == me["id"]
    assert again.json()["id"] == me["id"]
    assert other.json()["id"] != me["id"]
    assert admin.json()["is_admin"] is True


def test_me_without_db_or_header_is_demo_user(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRATION_DB_DSN", raising=False)
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    resp = asyncio.run(_get(_final_answer_app(), "/me"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "demo-user"


def test_me_fails_closed_without_identity_when_db(monkeypatch) -> None:
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.delenv("PLANNER_ALLOW_ANON", raising=False)
    resp = asyncio.run(_get(_final_answer_app(), "/me"))
    assert resp.status_code == 401, resp.text


def test_me_with_db_returns_users_row(monkeypatch) -> None:
    from planner_agent.main_agent import db

    pool = _FakeUsersPool()
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.setattr(db, "_pool", pool)
    asyncio.run(_me_with_db(pool))


async def _me_with_db(pool: _FakeUsersPool) -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = await client.get("/me", headers={"mail": "erin@example.com"})
        again = await client.get("/me", headers={"mail": "erin@example.com"})
    assert first.status_code == 200, first.text
    row = pool.users["erin@example.com"]
    assert first.json() == {"id": str(row["id"]), "email": "erin@example.com", "is_admin": False}
    assert again.json()["id"] == first.json()["id"]
