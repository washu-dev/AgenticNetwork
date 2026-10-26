from __future__ import annotations

import asyncio
import contextlib
import gzip
import http.server
import threading
import uuid
from pathlib import Path

import httpx
import pytest

from agentnet.directory.card_oasf import load_agent_card
from agentnet.directory.client import DirectoryClient

from chat_app.store import conversation_store
from chat_app.planner.config import PlannerConfig
from chat_app.planner.llm_client import ScriptedLLMClient, ToolCall
from chat_app.planner.models import Channel, ConversationContext
from chat_app.server import create_app
from chat_app.planner.memory_store import MemoryStore
from chat_app.tests.test_runtime import FailA2A, EMAIL_CARD, SpyA2A, _geospatial_card


def _config() -> PlannerConfig:
    return PlannerConfig(llm_enabled=False, llm_model="mock", fake_ads=True)


def _no_anon_opt_in(monkeypatch) -> None:
    # The anonymous-access opt-in, under its name and its old name.
    for name in ("CHAT_APP_ALLOW_ANON", "PLANNER_ALLOW_ANON"):
        monkeypatch.delenv(name, raising=False)


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
    monkeypatch.setenv("CHAT_APP_UPLOAD_DIR", str(tmp_path))
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
    # test_runtime.py's image-upload assertion.
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
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    _no_anon_opt_in(monkeypatch)
    resp = asyncio.run(_post_message())
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("env", [
    {"REGISTRATION_DB_DSN": "postgres://unused"},
    # An empty DATABASE_URL counts as unset; it does not hide the old name.
    {"DATABASE_URL": "", "REGISTRATION_DB_DSN": "postgres://unused"},
])
def test_old_db_name_still_fails_closed(monkeypatch, env) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _no_anon_opt_in(monkeypatch)
    resp = asyncio.run(_post_message())
    assert resp.status_code == 401, resp.text


def test_missing_identity_allowed_without_db(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _no_anon_opt_in(monkeypatch)
    resp = asyncio.run(_post_message())
    assert resp.status_code == 200, resp.text


def test_allow_anon_flag_overrides_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    _no_anon_opt_in(monkeypatch)
    monkeypatch.setenv("CHAT_APP_ALLOW_ANON", "1")
    resp = asyncio.run(_post_message())
    assert resp.status_code == 200, resp.text


def test_old_allow_anon_name_still_overrides_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    _no_anon_opt_in(monkeypatch)
    monkeypatch.setenv("PLANNER_ALLOW_ANON", "1")
    resp = asyncio.run(_post_message())
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("name", ["CHAT_APP_ALLOW_ANON", "PLANNER_ALLOW_ANON"])
@pytest.mark.parametrize("value", ["true", "yes", "on", "0", "", " 1"])
def test_allow_anon_needs_exactly_1(monkeypatch, name, value) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    _no_anon_opt_in(monkeypatch)
    monkeypatch.setenv(name, value)
    resp = asyncio.run(_post_message())
    assert resp.status_code == 401, resp.text


def test_new_allow_anon_name_wins_over_the_old_one(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    monkeypatch.setenv("CHAT_APP_ALLOW_ANON", "0")
    monkeypatch.setenv("PLANNER_ALLOW_ANON", "1")
    resp = asyncio.run(_post_message())
    assert resp.status_code == 401, resp.text


def test_shib_header_becomes_user_id(monkeypatch) -> None:
    # No DB configured, so credential injection is skipped; the header still sets identity.
    monkeypatch.delenv("DATABASE_URL", raising=False)
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
# P4f — admin via ADMIN_EMAILS allowlist (SSO identity, no shared token).
# The allowlist helpers are tested in agentnet/tests/test_identity.py.
# ---------------------------------------------------------------------------

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


def test_admin_health_reset_is_admin_only(monkeypatch) -> None:
    # It clears the chat app's agent health cache (it never reset the directory,
    # which is why it is no longer under /admin/ads/).
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    asyncio.run(_admin_health_reset())


async def _admin_health_reset() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        admin = await client.post("/admin/health/reset", headers={"mail": "admin@example.com"})
        assert admin.status_code == 200, admin.text
        assert admin.json() == {"ok": True}
        other = await client.post("/admin/health/reset", headers={"mail": "bob@example.com"})
        assert other.status_code == 403, other.text
        old = await client.post("/admin/ads/reset", headers={"mail": "admin@example.com"})
        assert old.status_code == 404, old.text


@pytest.mark.parametrize("db_url", [None, "postgres://unused"], ids=["no-db", "db-with-anon-opt-in"])
def test_the_anonymous_stand_in_is_never_admin(monkeypatch, db_url) -> None:
    # A request without an identity header runs as demo-user when anonymous access is
    # allowed. Listing that name in ADMIN_EMAILS must not make such callers admins.
    _no_anon_opt_in(monkeypatch)
    if db_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", db_url)
        monkeypatch.setenv("CHAT_APP_ALLOW_ANON", "1")
    monkeypatch.setenv("ADMIN_EMAILS", "demo-user")
    asyncio.run(_the_anonymous_stand_in_is_never_admin(check_me=db_url is None))


async def _the_anonymous_stand_in_is_never_admin(check_me: bool) -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        stats = await client.get("/admin/stats")
        assert stats.status_code == 403, stats.text
        approve = await client.post("/register/agents/a1/approve")
        assert approve.status_code == 403, approve.text
        if check_me:  # /me needs the database when one is configured
            me = (await client.get("/me")).json()
            assert me["email"] == "demo-user" and me["is_admin"] is False


def test_admin_route_401_without_identity_when_db(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    _no_anon_opt_in(monkeypatch)
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
    # POST /agents/{id}/handler is the code-upload step an agent's owner performs
    # (the registration service checks the owner) — a non-admin must NOT be
    # blocked at the gate. It shares its path with the admin-only GET review,
    # so a method-agnostic suffix match wrongly returned 403 here (the reported bug).
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    # Dead upstream: the proxy forwarding the request (rather than rejecting it)
    # is what proves the request cleared the admin gate. The failed connection
    # answers 503.
    monkeypatch.setenv("REGISTRATION_SERVICE_URL", "http://127.0.0.1:9")
    asyncio.run(_register_proxy_allows_non_admin_handler_upload())


async def _register_proxy_allows_non_admin_handler_upload() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/register/agents/some-id/handler",
            headers={"mail": "bob@example.com"},
            content=b"code",
        )
    assert resp.status_code == 503 and "could not be reached" in resp.json()["detail"], resp.text


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


class _GzipRegistration(http.server.BaseHTTPRequestHandler):
    """A registration service stand-in that replies gzip-encoded."""

    body = b'[{"id": "a1", "name": "echo"}]' * 20
    accept_encoding: list[str | None] = []

    def do_GET(self) -> None:
        type(self).accept_encoding.append(self.headers.get("Accept-Encoding"))
        payload = gzip.compress(self.body)
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Location", "/agents/a1")
        self.send_header("Set-Cookie", "a=1")
        self.send_header("Set-Cookie", "b=2")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


class _RecordingRegistration(http.server.BaseHTTPRequestHandler):
    """A registration service stand-in that records each request's method, path
    and forwarded identity, and every identity header it received."""

    seen: list[tuple[str, str, str | None]] = []
    identity_headers: list[list[tuple[str, str]]] = []

    def _reply(self) -> None:
        type(self).seen.append((self.command, self.path, self.headers.get("mail")))
        type(self).identity_headers.append([
            (name.lower(), value) for name, value in self.headers.items()
            if name.lower().replace("_", "-") in ("mail", "eppn", "remote-user")
        ])
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = do_DELETE = _reply

    def log_message(self, *args) -> None:
        pass


def test_register_proxy_forwards_the_callers_identity(monkeypatch) -> None:
    # The registration service records the caller's identity as an agent's owner and
    # checks it on delete, restart and code upload. The chat app leaves those checks
    # to it, so the pass-through must forward the identity header.
    _RecordingRegistration.seen = []
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RecordingRegistration)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("REGISTRATION_SERVICE_URL", f"http://127.0.0.1:{upstream.server_port}")
        asyncio.run(_register_proxy_forwards_the_callers_identity())
    finally:
        upstream.shutdown()
        upstream.server_close()
    assert _RecordingRegistration.seen == [
        ("POST", "/agents", "bob@example.com"),
        ("POST", "/agents/a1/handler", "bob@example.com"),
        ("POST", "/agents/a1/restart", "bob@example.com"),
        ("DELETE", "/agents/a1", "bob@example.com"),
    ]


async def _register_proxy_forwards_the_callers_identity() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        for method, path in (
            ("POST", "/register/agents"),
            ("POST", "/register/agents/a1/handler"),
            ("POST", "/register/agents/a1/restart"),
            ("DELETE", "/register/agents/a1"),
        ):
            resp = await client.request(
                method, path, headers={"mail": "bob@example.com"},
                content=b"{}" if method == "POST" else None,
            )
            assert resp.status_code == 200, (path, resp.text)


def test_register_proxy_forwards_only_the_identity_it_read(monkeypatch) -> None:
    # The caller's own identity headers are dropped and the identity the chat app read
    # is forwarded as one mail header, so the registration service cannot see a
    # different caller (two mail headers, eppn, another spelling). A request without a
    # header identity forwards none: never the anonymous stand-in.
    monkeypatch.delenv("DATABASE_URL", raising=False)  # anonymous allowed
    _RecordingRegistration.seen = []
    _RecordingRegistration.identity_headers = []
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RecordingRegistration)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("REGISTRATION_SERVICE_URL", f"http://127.0.0.1:{upstream.server_port}")
        asyncio.run(_register_proxy_forwards_only_the_identity_it_read())
    finally:
        upstream.shutdown()
        upstream.server_close()
    assert _RecordingRegistration.identity_headers == [
        [("mail", "first@example.com")],
        [("mail", "bob@example.com")],
        [("mail", "carol")],
        [],
        [],
    ]


async def _register_proxy_forwards_only_the_identity_it_read() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        for headers in (
            [("mail", "First@Example.com"), ("mail", "second@example.com"), ("eppn", "x@example.com")],
            [("mail", ""), ("eppn", "Bob@Example.com"), ("remote_user", "mallory@example.com")],
            [("Remote-User", "Carol"), ("REMOTE_USER", "mallory@example.com")],
            [("remote_user", "mallory@example.com"), ("mail", "")],
            [],
        ):
            resp = await client.post("/register/agents", headers=headers, content=b"{}")
            assert resp.status_code == 200, (headers, resp.text)


def test_register_proxy_returns_headers_that_match_the_body_it_sends(monkeypatch) -> None:
    # The pass-through sends the decoded body, so it drops the upstream's encoding,
    # length and hop-by-hop headers and keeps the rest (each Set-Cookie separately).
    # It asks upstream only for encodings httpx can decode, whatever the browser accepts.
    _GzipRegistration.accept_encoding = []
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GzipRegistration)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("REGISTRATION_SERVICE_URL", f"http://127.0.0.1:{upstream.server_port}")
        resp = asyncio.run(_get(_final_answer_app(), "/register/agents",
                                {"accept-encoding": "gzip, deflate, br, zstd"}))
    finally:
        upstream.shutdown()
        upstream.server_close()
    [sent] = _GzipRegistration.accept_encoding
    assert "br" not in sent and "zstd" not in sent, sent
    assert resp.status_code == 201
    assert resp.content == _GzipRegistration.body
    assert "content-encoding" not in resp.headers
    assert "connection" not in resp.headers
    assert resp.headers["content-length"] == str(len(_GzipRegistration.body))
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["location"] == "/agents/a1"
    assert resp.headers.get_list("set-cookie") == ["a=1", "b=2"]


def test_register_proxy_drops_hop_by_hop_request_headers(monkeypatch) -> None:
    # The pass-through re-sends the body it read with its own Content-Length, so a
    # chunked request's Transfer-Encoding must not go with it (a server may refuse a
    # request that carries both), nor any other header that describes only the
    # caller's connection, including one its Connection header names.
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    def client(*args, **kwargs):
        if kwargs.get("base_url"):  # the /register pass-through client
            kwargs["transport"] = httpx.MockTransport(upstream)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    app = _final_answer_app()
    monkeypatch.setattr(httpx, "AsyncClient", real_client)

    async def chunks():
        yield b'{"name": '
        yield b'"echo"}'

    async def register():
        async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/register/agents", content=chunks(), headers={
                "mail": "bob@example.com", "content-type": "application/json",
                "connection": "keep-alive, x-hop", "x-hop": "1", "keep-alive": "timeout=5",
                "te": "trailers", "trailer": "x-sum", "upgrade": "h2c",
                "proxy-authorization": "Basic eDp5", "x-kept": "1",
            })

    resp = asyncio.run(register())
    assert resp.status_code == 200, resp.text
    [sent] = seen
    received = {name.lower() for name in sent.headers}
    assert not received & {"transfer-encoding", "x-hop", "keep-alive", "te", "trailer",
                           "upgrade", "proxy-authorization"}, sent.headers
    assert "x-hop" not in sent.headers.get("connection", "")  # httpx's own keep-alive only
    assert sent.content == b'{"name": "echo"}'
    assert sent.headers["content-length"] == str(len(sent.content))
    assert (sent.headers["content-type"], sent.headers["x-kept"]) == ("application/json", "1")
    assert sent.headers.get_list("mail") == ["bob@example.com"]


@pytest.mark.parametrize(
    "timeout, status, says",
    [
        (httpx.ReadTimeout, 504, "still working on this request"),
        (httpx.ConnectTimeout, 503, "nothing was changed"),
        (httpx.PoolTimeout, 503, "nothing was changed"),
        (httpx.ConnectError, 503, "nothing was changed"),
    ],
)
def test_register_proxy_timeout_says_whether_the_request_was_received(
    monkeypatch, timeout, status, says
) -> None:
    # One agent's changes run one at a time in the registration service, so a
    # request it received can wait there longer than the pass-through's timeout
    # and still complete (504). A request that never reached it changed nothing
    # (503). Either way the UI shows a JSON message, not a bare 500.
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    real_client = httpx.AsyncClient

    def upstream(request: httpx.Request) -> httpx.Response:
        raise timeout("timed out", request=request)

    def client(*args, **kwargs):
        if kwargs.get("base_url"):  # the /register pass-through client
            kwargs["transport"] = httpx.MockTransport(upstream)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    app = _final_answer_app()
    monkeypatch.setattr(httpx, "AsyncClient", real_client)

    async def reject():
        async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(
                "/register/agents/00000000-0000-0000-0000-000000000001/reject",
                json={"reason": "x"}, headers={"mail": "admin@example.com"},
            )

    resp = asyncio.run(reject())
    assert resp.status_code == status, resp.text
    assert says in resp.json()["detail"]


def test_chat_ui_lists_every_agent_with_uploaded_code_for_approval() -> None:
    # Only agents that ship code (custom, llm with uploaded tools, legacy
    # tool_loop) are ever unapproved, so the approvals list filters on approval
    # and an uploaded handler alone, like the registration page. A custom-only
    # filter would hide the other two.
    # A source check of the page's script (the suite runs no JavaScript).
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()
    assert "(agents || []).filter(a => !a.approved && a.has_handler)" in html
    assert "a.handler_type === 'custom' && !a.approved" not in html


def test_chat_ui_approves_only_the_code_revision_it_showed() -> None:
    # An approval names the code the admin was shown in the code viewer; without
    # one, Approve opens the viewer instead of approving. The registration
    # service refuses it (409) if newer code was uploaded after that.
    # A source check of the page's script (the suite runs no JavaScript).
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()
    approve = html.split("async function approveAgentReq(id, btn) {", 1)[1].split("\n}\n", 1)[0]
    assert "const revision = _shownRevision[id];" in approve
    assert "toggleApprovalCode(id)" in approve
    assert "body: JSON.stringify({ revision })" in approve
    assert "if (pre.isConnected) _shownRevision[id] = data.revision;" in html
    # Reopened after the code changed: the earlier review (for an archive, the
    # download) no longer covers it, so the stored revision is dropped.
    toggle = html.split("async function toggleApprovalCode(id) {", 1)[1].split("\n}\n", 1)[0]
    assert "if (prev && prev !== data.revision) {\n      delete _shownRevision[id];" in toggle
    assert "Download it again and review it" in toggle
    # A registration service that reports no revision (older than this page)
    # cannot be approved from here; the viewer says why instead of storing none.
    assert "if (!data.revision) {" in toggle
    assert "reports no code revision" in toggle
    assert "Agents with uploaded code awaiting admin review" in html
    assert "Custom uploaded agents awaiting admin review" not in html


# ---------------------------------------------------------------------------
# P4e — public landing at / , hard-gated chat UI at /app
# ---------------------------------------------------------------------------

def test_landing_is_public_and_links_to_app(monkeypatch) -> None:
    # Even with a DB configured (fail-closed identity), / needs no identity.
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    _no_anon_opt_in(monkeypatch)
    asyncio.run(_landing_is_public())


async def _landing_is_public() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/")
    assert resp.status_code == 200, resp.text
    assert "Sign in with WashU" in resp.text
    assert 'href="/app"' in resp.text


def test_app_hard_gated_when_db_configured(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    _no_anon_opt_in(monkeypatch)
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
        assert "<title>AgenticNetwork</title>" in ok.text
        assert 'id="uv-status-pill"' in ok.text  # the chat UI, not the landing page


def test_app_served_in_dev_without_db(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _no_anon_opt_in(monkeypatch)
    asyncio.run(_app_served_in_dev())


async def _app_served_in_dev() -> None:
    app = _final_answer_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/app")
    assert resp.status_code == 200, resp.text
    assert "<title>AgenticNetwork</title>" in resp.text
    assert 'id="uv-status-pill"' in resp.text  # the chat UI, not the landing page


# ---------------------------------------------------------------------------
# H2 — ownership on data endpoints (multi-user hardening)
# ---------------------------------------------------------------------------

def test_conversation_endpoints_enforce_ownership(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
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
    store = MemoryStore()
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
    monkeypatch.delenv("DATABASE_URL", raising=False)
    asyncio.run(_header_identity_wins_over_body_user_id())


async def _header_identity_wins_over_body_user_id() -> None:
    store = MemoryStore()
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
    monkeypatch.delenv("DATABASE_URL", raising=False)
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
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _no_anon_opt_in(monkeypatch)
    asyncio.run(_body_user_id_kept_without_identity_header())


async def _body_user_id_kept_without_identity_header() -> None:
    store = MemoryStore()
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
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    _no_anon_opt_in(monkeypatch)
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
    monkeypatch.delenv("DATABASE_URL", raising=False)
    asyncio.run(_confirmations_forbid_non_owner())


async def _confirmations_forbid_non_owner() -> None:
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    store = MemoryStore()
    store.save_context(
        ConversationContext(
            context_id="c-alice", user_id="alice@example.com", channel=Channel.api, current_goal="send it"
        )
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
    monkeypatch.delenv("DATABASE_URL", raising=False)
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
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _no_anon_opt_in(monkeypatch)
    resp = asyncio.run(_get(_final_answer_app(), "/me"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "demo-user"


def test_me_fails_closed_without_identity_when_db(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    _no_anon_opt_in(monkeypatch)
    resp = asyncio.run(_get(_final_answer_app(), "/me"))
    assert resp.status_code == 401, resp.text


def test_me_with_db_returns_users_row(monkeypatch) -> None:
    from chat_app.store import db

    pool = _FakeUsersPool()
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
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
