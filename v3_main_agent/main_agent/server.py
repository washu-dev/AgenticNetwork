from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Sequence

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.common.models import Channel
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.state.store import InMemoryStateStore

from v3_main_agent.main_agent.config import MainAgentConfig, load_config
from v3_main_agent.main_agent.llm_client import LLMClient, OpenAIToolLoopClient
from v3_main_agent.main_agent.runtime import MainAgentRuntime

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

    app = FastAPI(title="V3 Main Agent", version="0.1.0")
    app.state.config = cfg
    app.state.runtime = runtime
    app.state.directory = directory
    app.state.a2a = a2a
    app.state.store = store

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "fake_ads": str(cfg.fake_ads), "model": cfg.llm_model}

    @app.get("/", response_class=HTMLResponse)
    def chat_ui() -> HTMLResponse:
        return HTMLResponse(_load_chat_html())

    @app.get("/agents")
    def get_agents() -> dict[str, Any]:
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
    async def post_message(msg: IncomingMessage) -> dict[str, Any]:
        try:
            result = await runtime.handle_message(
                user_id=msg.user_id,
                channel=msg.channel,
                text=msg.text,
                context_id=msg.context_id,
                metadata=msg.metadata,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("main_agent_message_failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return result.to_response()

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
