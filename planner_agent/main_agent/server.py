from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import mimetypes
import os
import re
import time as _time
from pathlib import Path
from typing import Annotated, Any, Sequence
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from planner_agent.main_agent.a2a.client import A2AClient
from planner_agent.main_agent.models import Channel
from planner_agent.main_agent.directory.card_oasf_converter import load_agent_card
from planner_agent.main_agent.directory.client import DirectoryClient
from planner_agent.main_agent.state.store import InMemoryStateStore

from planner_agent.main_agent.auth import get_current_user, get_shib_email
from planner_agent.main_agent.auth_router import router as auth_router
from planner_agent.main_agent.credentials_router import (
    grants_router,
    router as credentials_router,
)
from planner_agent.main_agent.config import MainAgentConfig, load_config
from planner_agent.main_agent.db import close_db, fetch_user_credentials, init_db
from planner_agent.main_agent.llm_client import LLMClient, OpenAIToolLoopClient
from planner_agent.main_agent.credential_gate import DbCredentialGate
from planner_agent.main_agent.runtime import MainAgentRuntime
from planner_agent.main_agent import conversation_store

logger = logging.getLogger(__name__)

class IncomingMessage(BaseModel):
    user_id: str = Field(default="demo-user")
    channel: Channel = Channel.api
    text: str
    context_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConfirmationRequest(BaseModel):
    user_id: str = Field(default="demo-user")
    channel: Channel = Channel.api
    context_id: str
    confirmed: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackRequest(BaseModel):
    context_id: str | None = None
    rating: str                   # "up" or "down"
    comment: str | None = None
    agent_id: str | None = None
    skill: str | None = None
    prompt_text: str | None = None


def create_app(
    *,
    config: MainAgentConfig | None = None,
    directory_client: DirectoryClient | None = None,
    a2a_client: A2AClient | None = None,
    state_store: InMemoryStateStore | None = None,
    llm_client: LLMClient | None = None,
    agent_card_paths: Sequence[str | Path] | None = None,
) -> FastAPI:
    cfg = config or load_config()
    directory = directory_client or DirectoryClient(
        fake_ads=cfg.fake_ads,
        ads_url=cfg.ads_server_addr,
    )
    a2a = a2a_client or A2AClient()
    store = state_store or InMemoryStateStore()
    if llm_client is None:
        if not cfg.llm_enabled:
            raise RuntimeError(
                "MAIN_AGENT_LLM_ENABLED=false but no llm_client was provided. "
                "Pass a ScriptedLLMClient for tests or enable LLM mode."
            )
        llm_client = OpenAIToolLoopClient(cfg)

    # P4c: per-agent credential grants are DB-backed; without a DB there's nothing
    # to enforce against, so the gate is None and enforcement is a pass-through.
    credential_gate = DbCredentialGate() if os.getenv("REGISTRATION_DB_DSN") else None

    runtime = MainAgentRuntime(
        config=cfg,
        directory_client=directory,
        a2a_client=a2a,
        state_store=store,
        llm_client=llm_client,
        credential_gate=credential_gate,
    )

    # In fake-ADS mode, optionally pre-publish Agent Cards into the in-process directory.
    for card_path in _agent_card_paths(agent_card_paths, allow_local=cfg.fake_ads):
        directory.publish_agent_card(load_agent_card(card_path))

    # Ring buffer of the last 200 requests. Each entry is a dict.
    _request_log: collections.deque = collections.deque(maxlen=200)
    _feedback_log: collections.deque = collections.deque(maxlen=500)
    _agent_health: dict[str, dict] = {}
    # user_id → deque of { context_id, title, started_at, last_active_at }
    _session_index: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=20))

    _ADMIN_TOKEN = os.getenv("MAIN_AGENT_ADMIN_TOKEN", "")

    def _require_admin(request: Request) -> None:
        if not _ADMIN_TOKEN:
            return
        token = request.headers.get("X-Admin-Token", "")
        if token != _ADMIN_TOKEN:
            raise HTTPException(status_code=401, detail="Admin token required")

    app = FastAPI(title="Planner Agent", version="0.1.0")
    app.state.config = cfg
    app.state.runtime = runtime
    app.state.directory = directory
    app.state.a2a = a2a
    app.state.store = store

    @app.post("/admin/login")
    async def admin_login(payload: dict[str, str]) -> dict[str, Any]:
        if _ADMIN_TOKEN and payload.get("password") != _ADMIN_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return {"ok": True, "token": _ADMIN_TOKEN or "open"}

    # Conversation history store (SQLite, always active).
    _conv_db_path = os.getenv(
        "MAIN_AGENT_CONVERSATION_DB",
        "/tmp/planner_agent_conversations.db",
    )
    conversation_store.init(_conv_db_path)

    # DB lifecycle (skipped when REGISTRATION_DB_DSN is unset, e.g. in unit tests).
    if os.getenv("REGISTRATION_DB_DSN"):
        @app.on_event("startup")
        async def _startup_db():
            await init_db()

        @app.on_event("shutdown")
        async def _shutdown_db():
            await close_db()

    # Auth + credential routers (active whenever the DB is configured).
    if os.getenv("REGISTRATION_DB_DSN"):
        app.include_router(auth_router)
        app.include_router(credentials_router)
        app.include_router(grants_router)

    @app.get("/me")
    async def me(user: Annotated[dict, Depends(get_current_user)]) -> dict[str, Any]:
        return {"email": user["email"]}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "fake_ads": str(cfg.fake_ads), "model": cfg.llm_model}

    _reg_base = os.getenv("REGISTRATION_SERVICE_URL", "http://localhost:8091")
    _reg_client = httpx.AsyncClient(base_url=_reg_base, timeout=30.0)

    @app.api_route("/register/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy_registration(path: str, request: Request) -> Response:
        url = f"/{path}"
        body = await request.body()
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "content-length")}
        resp = await _reg_client.request(
            request.method, url,
            content=body, headers=headers, params=request.query_params,
        )
        return Response(content=resp.content, status_code=resp.status_code,
                        headers=dict(resp.headers))

    @app.get("/", response_class=HTMLResponse)
    async def chat_ui() -> HTMLResponse:
        return HTMLResponse(_load_chat_html())

    @app.get("/agents")
    async def get_agents() -> dict[str, Any]:
        try:
            agents = directory.search_agents()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"ADS unavailable: {exc}") from exc
        return {
            "count": len(agents),
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "name": a.name,
                    "organization": a.organization,
                    "description": a.description,
                    "endpoint": a.endpoint,
                    "skills": sorted({s for cap in a.skills for s in cap.skill_tags + cap.custom_skill_tags}),
                }
                for a in agents
            ],
        }

    @app.post("/messages")
    async def post_message(request: Request) -> dict[str, Any]:
        msg = await _incoming_message_from_request(request)
        metadata = dict(msg.metadata)
        await _inject_credentials(request, metadata)
        try:
            t0 = _time.monotonic()
            result = await runtime.handle_message(
                user_id=msg.user_id,
                channel=msg.channel,
                text=msg.text,
                context_id=msg.context_id,
                metadata=metadata,
            )
            latency_ms = int((_time.monotonic() - t0) * 1000)
            _request_log.appendleft({
                "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                "prompt": msg.text[:80],
                "routed_to": result.selected_agents[-1]["agent_id"] if result.selected_agents else "planner",
                "latency_ms": latency_ms,
                "state": result.state,
            })
            now_ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
            sessions = _session_index[msg.user_id]
            existing = next((s for s in sessions if s["context_id"] == result.context_id), None)
            if existing:
                existing["last_active_at"] = now_ts
            else:
                sessions.appendleft({
                    "context_id": result.context_id,
                    "title": msg.text[:60],
                    "started_at": now_ts,
                    "last_active_at": now_ts,
                })
            try:
                ctx = store.get_context(result.context_id)
                await conversation_store.save(result.context_id, ctx.conversation_history)
            except Exception:
                pass
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("main_agent_message_failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return result.to_response()

    @app.post("/messages/stream")
    async def post_message_stream(request: Request) -> StreamingResponse:
        msg = await _incoming_message_from_request(request)
        metadata = dict(msg.metadata)
        await _inject_credentials(request, metadata)

        async def event_stream():
            queue: asyncio.Queue[tuple[str, dict[str, Any] | None]] = asyncio.Queue()

            async def progress(entry: dict[str, Any]) -> None:
                await queue.put(("trace", entry))

            async def run_message() -> None:
                try:
                    t0 = _time.monotonic()
                    result = await runtime.handle_message(
                        user_id=msg.user_id,
                        channel=msg.channel,
                        text=msg.text,
                        context_id=msg.context_id,
                        metadata=metadata,
                        progress_callback=progress,
                    )
                    latency_ms = int((_time.monotonic() - t0) * 1000)
                    now_ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
                    _request_log.appendleft({
                        "timestamp": now_ts,
                        "prompt": msg.text[:80],
                        "routed_to": result.selected_agents[-1]["agent_id"] if result.selected_agents else "planner",
                        "latency_ms": latency_ms,
                        "state": result.state,
                    })
                    sessions = _session_index[msg.user_id]
                    existing = next((s for s in sessions if s["context_id"] == result.context_id), None)
                    if existing:
                        existing["last_active_at"] = now_ts
                    else:
                        sessions.appendleft({
                            "context_id": result.context_id,
                            "title": msg.text[:60],
                            "started_at": now_ts,
                            "last_active_at": now_ts,
                        })
                    try:
                        ctx = store.get_context(result.context_id)
                        await conversation_store.save(result.context_id, ctx.conversation_history)
                    except Exception:
                        pass
                    await queue.put(("final", result.to_response()))
                except asyncio.CancelledError:
                    await queue.put(("error", {"detail": "Request was cancelled or timed out."}))
                    raise
                except Exception as exc:
                    logger.exception("main_agent_message_stream_failed")
                    await queue.put(("error", {"detail": str(exc)}))
                finally:
                    await queue.put(("done", None))

            task = asyncio.create_task(run_message())
            try:
                get_task: asyncio.Task[tuple[str, dict[str, Any] | None]] = asyncio.ensure_future(queue.get())
                while True:
                    done, _ = await asyncio.wait({get_task}, timeout=15.0)
                    if not done:
                        yield ": heartbeat\n\n"
                        continue
                    event_name, payload = get_task.result()
                    if event_name == "done":
                        break
                    yield _sse(event_name, payload or {})
                    get_task = asyncio.ensure_future(queue.get())
            finally:
                get_task.cancel()
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.post("/confirmations")
    async def post_confirmation(req: ConfirmationRequest, request: Request) -> dict[str, Any]:
        # P4c: the confirm button must carry the same per-user credentials +
        # auth_user_id as /messages, or a credential-consent resume dispatches
        # with no key and records no grant.
        await _inject_credentials(request, req.metadata)
        pending = store.get_pending_confirmation(req.context_id) or {}
        if not req.confirmed:
            cancel = runtime._cancel_confirmation(req.context_id, pending)
            return cancel.to_response()
        if pending.get("kind") == "credential_consent":
            result = await runtime._resume_credential_consent(
                req.context_id, req.user_id, req.channel, req.metadata, pending
            )
        else:
            result = await runtime._resume_confirmation(
                req.context_id, req.user_id, req.channel, req.metadata
            )
        return result.to_response()

    @app.post("/feedback")
    async def post_feedback(req: FeedbackRequest) -> dict[str, bool]:
        _feedback_log.appendleft({
            "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            "rating": req.rating,
            "comment": req.comment or "",
            "agent_id": req.agent_id or "",
            "skill": req.skill or "",
            "prompt_text": req.prompt_text or "",
        })
        return {"ok": True}

    @app.get("/admin/stats")
    async def admin_stats(request: Request, _: None = Depends(_require_admin)) -> dict[str, Any]:
        try:
            agents = directory.search_agents()
            agent_count = len(agents)
            ads_reachable = True
        except Exception:
            # Distinguish "ADS unreachable" from a genuine zero-agent directory so
            # the dashboard doesn't silently report 0 during an ADS blip.
            agent_count = None
            ads_reachable = False

        today_prefix = _time.strftime("%Y-%m-%d", _time.gmtime())
        today_reqs = [r for r in _request_log if r["timestamp"].startswith(today_prefix)]
        latencies = [r["latency_ms"] for r in today_reqs]
        sorted_lat = sorted(latencies)

        pos = sum(1 for f in _feedback_log if f["rating"] == "up")
        neg = sum(1 for f in _feedback_log if f["rating"] == "down")

        return {
            "agents_connected": agent_count,
            "ads_reachable": ads_reachable,
            "requests_today": len(today_reqs),
            "last_request_ts": _request_log[0]["timestamp"] if _request_log else None,
            "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
            "p95_latency_ms": int(sorted_lat[int(len(sorted_lat) * 0.95)]) if sorted_lat else 0,
            "feedback_total": len(_feedback_log),
            "feedback_positive": pos,
            "feedback_negative": neg,
        }

    @app.get("/admin/traces")
    async def admin_traces(request: Request, _: None = Depends(_require_admin)) -> dict[str, Any]:
        rows = [
            {
                "timestamp": entry["timestamp"],
                "prompt": entry["prompt"],
                "routed_to": entry["routed_to"],
                "latency_ms": entry["latency_ms"],
                "state": entry["state"],
            }
            for entry in _request_log
        ]
        return {"traces": rows}

    @app.get("/admin/feedback")
    async def admin_feedback(request: Request, _: None = Depends(_require_admin)) -> dict[str, Any]:
        items = list(_feedback_log)
        pos = sum(1 for f in items if f["rating"] == "up")
        return {
            "total": len(items),
            "positive": pos,
            "negative": len(items) - pos,
            "items": items,
        }

    async def _probe_agent(agent_id: str, endpoint: str) -> None:
        # Probe the canonical A2A liveness endpoint (the agent card) rather than
        # GET / — A2A servers only accept POST at "/", so GET / returns 405, which
        # spams agent logs and is a non-obvious "reachable" signal. The well-known
        # card path returns 200 when the agent is up. Any HTTP response (<500) still
        # counts as reachable, so agents on frameworks that don't serve that exact
        # path aren't falsely marked down.
        url = endpoint.rstrip("/") + "/.well-known/agent-card.json"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                t0 = _time.monotonic()
                r = await client.get(url)
                latency_ms = (_time.monotonic() - t0) * 1000
            reachable = r.status_code < 500
        except Exception:
            reachable = False
            latency_ms = 0.0

        prev = _agent_health.get(agent_id, {})
        if reachable:
            _agent_health[agent_id] = {
                "status": "online",
                "last_ok_ts": _time.monotonic(),
                "avg_latency_ms": int(latency_ms),
                "requests_today": prev.get("requests_today", 0),
            }
        else:
            last_ok = prev.get("last_ok_ts")
            elapsed = (_time.monotonic() - last_ok) if last_ok else float("inf")
            _agent_health[agent_id] = {
                "status": "degraded" if elapsed < 300 else "offline",
                "last_ok_ts": last_ok,
                "avg_latency_ms": prev.get("avg_latency_ms", 0),
                "requests_today": prev.get("requests_today", 0),
            }

    @app.on_event("startup")
    async def _start_health_probe_loop():
        async def _loop():
            while True:
                try:
                    agents = directory.search_agents()
                    tasks = [_probe_agent(a.agent_id, a.endpoint) for a in agents if a.endpoint]
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                except Exception:
                    pass
                await asyncio.sleep(60)
        asyncio.create_task(_loop())

    @app.get("/admin/agents/health")
    async def admin_agents_health(request: Request, _: None = Depends(_require_admin)) -> dict[str, Any]:
        try:
            agents = directory.search_agents()
            ads_reachable = True
        except Exception:
            agents = []
            ads_reachable = False
        rows = []
        for a in agents:
            h = _agent_health.get(a.agent_id, {"status": "unknown", "avg_latency_ms": 0, "requests_today": 0})
            last_ok = h.get("last_ok_ts")
            rows.append({
                "agent_id": a.agent_id,
                "name": a.name,
                "endpoint": a.endpoint,
                "status": h["status"],
                "last_ping_s": int(_time.monotonic() - last_ok) if last_ok else None,
                "avg_latency_ms": h["avg_latency_ms"],
                "requests_today": h["requests_today"],
            })
        return {"agents": rows, "ads_reachable": ads_reachable}

    @app.get("/sessions")
    async def get_sessions(user_id: str = "demo-user") -> dict[str, Any]:
        return {"sessions": list(_session_index.get(user_id, []))}

    @app.delete("/conversations/{context_id}")
    async def delete_conversation(context_id: str) -> dict[str, bool]:
        await conversation_store.delete(context_id)
        _session_index.pop(context_id, None)
        for sessions in _session_index.values():
            to_remove = [s for s in sessions if s["context_id"] == context_id]
            for s in to_remove:
                sessions.remove(s)
        return {"ok": True}

    @app.get("/conversations/{context_id}")
    async def get_conversation(context_id: str) -> dict[str, Any]:
        # Try SQLite first (survives restarts), fall back to in-memory store.
        messages = await conversation_store.load(context_id)
        if messages is None:
            # SQLite not initialised — fall back to in-memory
            try:
                ctx = store.get_context(context_id)
                messages = conversation_store._parse_history(ctx.conversation_history)
                messages = [{"role": r, "text": t} for r, t in messages]
            except KeyError:
                raise HTTPException(status_code=404, detail="Conversation not found")
        if not messages:
            # SQLite has no rows for this id — check in-memory as fallback
            try:
                ctx = store.get_context(context_id)
                parsed = conversation_store._parse_history(ctx.conversation_history)
                messages = [{"role": r, "text": t} for r, t in parsed]
            except KeyError:
                pass
        return {"context_id": context_id, "messages": messages}

    @app.post("/admin/ads/reset")
    async def admin_ads_reset(request: Request, _: None = Depends(_require_admin)) -> dict[str, bool]:
        if hasattr(directory, "_cache_ts"):
            directory._cache_ts = 0
        _agent_health.clear()
        return {"ok": True}

    @app.delete("/admin/agents/{agent_id}")
    async def admin_remove_agent(agent_id: str, request: Request, _: None = Depends(_require_admin)) -> dict[str, bool]:
        try:
            await asyncio.get_event_loop().run_in_executor(None, directory.unpublish_agent, agent_id)
        except Exception:
            logger.warning("admin_remove_agent_unpublish_failed agent_id=%s", agent_id, exc_info=True)
        _agent_health.pop(agent_id, None)
        logger.info("admin_removed_agent agent_id=%s", agent_id)
        return {"ok": True}

    @app.post("/admin/agents/{agent_id}/ping")
    async def admin_ping_agent(agent_id: str, request: Request, _: None = Depends(_require_admin)) -> dict[str, Any]:
        try:
            agents = directory.search_agents()
        except Exception:
            agents = []
        agent = next((a for a in agents if a.agent_id == agent_id), None)
        if not agent or not agent.endpoint:
            raise HTTPException(status_code=404, detail="Agent not found or has no endpoint")
        await _probe_agent(agent_id, agent.endpoint)
        h = _agent_health.get(agent_id, {})
        return {"agent_id": agent_id, "status": h.get("status", "unknown"), "avg_latency_ms": h.get("avg_latency_ms", 0)}

    return app


async def _refresh_google_token(creds: dict) -> dict:
    """Exchange the stored refresh_token for a fresh access_token."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                creds.get("token_uri", "https://oauth2.googleapis.com/token"),
                data={
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                    "refresh_token": creds["refresh_token"],
                    "grant_type": "refresh_token",
                },
                timeout=10,
            )
        if resp.status_code == 200:
            return {**creds, "access_token": resp.json()["access_token"]}
        logger.warning("google_token_refresh_failed status=%d", resp.status_code)
    except Exception:
        logger.warning("google_token_refresh_error", exc_info=True)
    return creds


async def _inject_credentials(request: Request, metadata: dict[str, Any]) -> None:
    """Fetch the current user's credentials and add them to metadata if auth is active."""
    if not os.getenv("REGISTRATION_DB_DSN"):
        return
    try:
        user = await get_current_user(request)
        # P4c: the runtime keys credential grants on the DB user UUID (grants live
        # under users.id, not the Shib email). Pass it through metadata.
        metadata["auth_user_id"] = str(user["id"])
        credentials = await fetch_user_credentials(user["id"])
        if credentials:
            if "google_oauth" in credentials and credentials["google_oauth"].get("refresh_token"):
                credentials["google_oauth"] = await _refresh_google_token(credentials["google_oauth"])
            metadata["credentials"] = credentials
    except Exception:
        pass


def _anon_identity_allowed() -> bool:
    """Whether an unauthenticated request may fall back to the ``demo-user`` identity.

    Fail-closed once a real deployment DB is configured: with ``REGISTRATION_DB_DSN``
    set, a missing Shibboleth header is a 401, not a silent collapse to a shared
    identity. Anonymous is allowed when there is no DB (Tier A/B laptop runs and unit
    tests) or when the operator explicitly opts in with ``PLANNER_ALLOW_ANON=1`` (local
    dev against a real DB).
    """
    if os.getenv("PLANNER_ALLOW_ANON") == "1":
        return True
    return not os.getenv("REGISTRATION_DB_DSN")


def _resolve_shib_user_id(request: Request) -> str:
    """Return the authenticated identity for a request, or fail closed.

    Uses the Shibboleth email when present. When it's missing, returns ``demo-user``
    only if :func:`_anon_identity_allowed`; otherwise re-raises the 401 from
    ``get_shib_email`` so anonymous callers can't be silently attributed to a shared
    identity (multi-user hardening F3).
    """
    try:
        return get_shib_email(request)
    except HTTPException:
        if _anon_identity_allowed():
            return "demo-user"
        raise


async def _incoming_message_from_request(request: Request) -> IncomingMessage:
    # Use the Shibboleth-authenticated email as user_id (fail-closed when a DB is set).
    shib_user_id = _resolve_shib_user_id(request)

    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        metadata = _parse_metadata_field(form.get("metadata"))
        uploads = []
        for key in ("image", "file", "attachment"):
            uploads.extend(form.getlist(key))
        uploaded_files = []
        for upload in uploads:
            if not (hasattr(upload, "filename") and hasattr(upload, "read")):
                continue
            uploaded_files.append(await _store_uploaded_file(upload))
        if uploaded_files:
            metadata["uploaded_files"] = uploaded_files
        text = str(form.get("text") or "").strip()
        if not text and uploaded_files:
            text = f"I've attached a file: {uploaded_files[0]['filename']}"
        context_id = _clean_optional_form_value(form.get("context_id"))
        return IncomingMessage(
            user_id=shib_user_id,
            channel=Channel(str(form.get("channel") or Channel.api.value)),
            text=text,
            context_id=context_id,
            metadata=metadata,
        )

    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Request body must be JSON or multipart/form-data.") from exc
    msg = IncomingMessage.model_validate(payload)
    # Override user_id with authenticated identity unless the caller explicitly set it.
    if msg.user_id == "demo-user":
        msg.user_id = shib_user_id
    return msg


def _parse_metadata_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean_optional_form_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"null", "undefined"}:
        return None
    return text


async def _store_uploaded_file(upload: Any) -> dict[str, Any]:
    filename = Path(str(upload.filename or "upload")).name
    content_type = (
        str(getattr(upload, "content_type", "") or "")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file was empty.")
    max_bytes = int(os.getenv("MAIN_AGENT_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Upload exceeds {max_bytes} bytes.")
    suffix = Path(filename).suffix.lower() or (mimetypes.guess_extension(content_type) or ".bin")
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(filename).stem).strip("._") or "upload"
    out_dir = Path(os.getenv("MAIN_AGENT_UPLOAD_DIR", "/tmp/planner_agent_uploads"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{uuid4().hex}_{safe_stem}{suffix}"
    path.write_bytes(data)
    return {
        "path": str(path),
        "filename": filename,
        "content_type": content_type,
        "size": len(data),
    }


def _agent_card_paths(
    paths: Sequence[str | Path] | None,
    *,
    allow_local: bool,
) -> list[Path]:
    if paths is not None:
        items = [Path(p) for p in paths]
        if items and not allow_local:
            raise RuntimeError(
                "Agent Card paths can only be loaded in fake-ADS mode. "
                "Publish them to the real ADS service instead."
            )
        return items
    if not allow_local:
        return []
    raw = os.getenv("MAIN_AGENT_CARD_PATHS", "")
    return [Path(item.strip()) for item in raw.split(",") if item.strip()]


_CHAT_HTML_PATH = Path(__file__).parent / "web" / "index.html"


def _load_chat_html() -> str:
    """Read the chat UI HTML from disk on each request.

    Reading on each request makes UI iteration cheap during development
    (no server restart). The file is small so disk overhead is fine.
    """
    return _CHAT_HTML_PATH.read_text(encoding="utf-8")


def _sse(event_name: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event_name}\ndata: {data}\n\n"
