from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
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
from planner_agent.main_agent.credentials_router import router as credentials_router
from planner_agent.main_agent.config import MainAgentConfig, load_config
from planner_agent.main_agent.db import close_db, fetch_user_credentials, init_db
from planner_agent.main_agent.llm_client import LLMClient, OpenAIToolLoopClient
from planner_agent.main_agent.runtime import MainAgentRuntime

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

    runtime = MainAgentRuntime(
        config=cfg,
        directory_client=directory,
        a2a_client=a2a,
        state_store=store,
        llm_client=llm_client,
    )

    # In fake-ADS mode, optionally pre-publish Agent Cards into the in-process directory.
    for card_path in _agent_card_paths(agent_card_paths, allow_local=cfg.fake_ads):
        directory.publish_agent_card(load_agent_card(card_path))

    app = FastAPI(title="Planner Agent", version="0.1.0")
    app.state.config = cfg
    app.state.runtime = runtime
    app.state.directory = directory
    app.state.a2a = a2a
    app.state.store = store

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
            result = await runtime.handle_message(
                user_id=msg.user_id,
                channel=msg.channel,
                text=msg.text,
                context_id=msg.context_id,
                metadata=metadata,
            )
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
                    result = await runtime.handle_message(
                        user_id=msg.user_id,
                        channel=msg.channel,
                        text=msg.text,
                        context_id=msg.context_id,
                        metadata=metadata,
                        progress_callback=progress,
                    )
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
    async def post_confirmation(req: ConfirmationRequest) -> dict[str, Any]:
        if not req.confirmed:
            pending = store.get_pending_confirmation(req.context_id) or {}
            cancel = runtime._cancel_confirmation(req.context_id, pending)
            return cancel.to_response()
        result = await runtime._resume_confirmation(
            req.context_id, req.user_id, req.channel, req.metadata
        )
        return result.to_response()

    return app


async def _inject_credentials(request: Request, metadata: dict[str, Any]) -> None:
    """Fetch the current user's credentials and add them to metadata if auth is active."""
    if not os.getenv("REGISTRATION_DB_DSN"):
        return
    try:
        user = await get_current_user(request)
        credentials = await fetch_user_credentials(user["id"])
        if credentials:
            metadata["credentials"] = credentials
    except Exception:
        pass


async def _incoming_message_from_request(request: Request) -> IncomingMessage:
    # Use the Shibboleth-authenticated email as user_id when available.
    try:
        shib_user_id = get_shib_email(request)
    except Exception:
        shib_user_id = "demo-user"

    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        metadata = _parse_metadata_field(form.get("metadata"))
        uploads = []
        for key in ("image", "file", "attachment"):
            uploads.extend(form.getlist(key))
        uploaded_images = []
        for upload in uploads:
            if not (hasattr(upload, "filename") and hasattr(upload, "read")):
                continue
            uploaded_images.append(await _store_uploaded_image(upload))
        if uploaded_images:
            metadata["uploaded_images"] = uploaded_images
        text = str(form.get("text") or "").strip()
        if not text and uploaded_images:
            text = "Analyze this image, infer its likely location, and describe what happened."
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


async def _store_uploaded_image(upload: Any) -> dict[str, Any]:
    filename = Path(str(upload.filename or "uploaded-image")).name
    content_type = (
        str(getattr(upload, "content_type", "") or "")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail=f"Unsupported upload type {content_type!r}; image/* required.")

    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded image was empty.")
    max_bytes = int(os.getenv("MAIN_AGENT_MAX_IMAGE_UPLOAD_BYTES", str(15 * 1024 * 1024)))
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Uploaded image exceeds {max_bytes} bytes.")

    suffix = Path(filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
        suffix = mimetypes.guess_extension(content_type) or ".img"
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(filename).stem).strip("._") or "image"
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
