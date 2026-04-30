#!/usr/bin/env python3
"""A2A wrapper for the autonomous Intelligence Agent.

This script exposes the intelligence agent as an A2A HTTP service so the PI
agent (or any other A2A client) can discover it through ADS and invoke it
remotely. The agent is itself an LLM that drives a tool-calling ReAct loop:

- ``discover_agents`` queries the AGNTCY Directory.
- ``query_peer_agent`` opens an A2A request to a discovered peer (typically
  a recon agent providing forward spider-robot mission status, or an OSINT
  scout agent).
- ``web_search`` calls the Brave Search API for external context the peer
  agents don't own.

The user's natural-language prompt is the only input — there is no
``metadata.skill`` routing today. The Agent Card's ``intelligence/*`` skills
exist for ADS discoverability so other agents can find this one by topic.

Examples:
  export INTEL_AGENT_MODEL=openai/gpt-5-mini
  export OPENAI_API_KEY=sk-...
  export BRAVE_API_KEY=BSA...

  # Start the A2A server on port 9105 (matches the published Agent Card).
  python intelligence_agent.py serve-a2a --host 0.0.0.0 --port 9105 \\
      --directory-server 10.229.117.37:8888

  # Run a one-shot CLI request without going through A2A.
  python intelligence_agent.py run-once \\
      --prompt "Give me a current situational picture for grid square ALPHA-3."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

import httpx

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

logger = logging.getLogger("intelligence_agent")

if TYPE_CHECKING:
    from a2a.server.agent_execution import RequestContext as A2ARequestContext
    from a2a.server.events import EventQueue as A2AEventQueue
    from a2a.server.tasks import TaskUpdater as A2ATaskUpdater
    from a2a.types import AgentCard as A2AAgentCard

try:
    from a2a.server.agent_execution import AgentExecutor as _AgentExecutorBase
    from a2a.server.apps import A2AStarletteApplication as _A2AStarletteApplication
    from a2a.server.request_handlers import DefaultRequestHandler as _DefaultRequestHandler
    from a2a.server.tasks import InMemoryTaskStore as _InMemoryTaskStore
    from a2a.server.tasks import TaskUpdater as _TaskUpdater
    from a2a.types import AgentCard as _AgentCard
    from a2a.types import DataPart as _DataPart
    from a2a.types import Part as _Part
    from a2a.types import TextPart as _TextPart
except ImportError as exc:
    A2A_IMPORT_ERROR: ImportError | None = exc
    _AgentExecutorBase = object
    _A2AStarletteApplication = None
    _DefaultRequestHandler = None
    _InMemoryTaskStore = None
    _TaskUpdater = None
    _AgentCard = None
    _DataPart = None
    _Part = None
    _TextPart = None
else:
    A2A_IMPORT_ERROR = None


DEFAULT_INTEL_A2A_CARD = (
    Path(__file__).resolve().parents[2]
    / "v2_agent_network"
    / "configs"
    / "agent_cards"
    / "intelligence_agent.card.json"
)

# Skill tags from the v2 card — kept here as constants so the module's
# `tools` subcommand can list them without re-parsing the card.
SKILL_SITUATION_SUMMARY = "intelligence/situation_summary"
SKILL_RISK_ASSESSMENT = "intelligence/risk_assessment"
SKILL_COURSE_OF_ACTION = "intelligence/course_of_action"
SKILL_ENTITY_TRACKING = "intelligence/entity_tracking"
SKILL_REPORT_GENERATION = "intelligence/report_generation"

_INTEL_SKILLS = (
    SKILL_SITUATION_SUMMARY,
    SKILL_RISK_ASSESSMENT,
    SKILL_COURSE_OF_ACTION,
    SKILL_ENTITY_TRACKING,
    SKILL_REPORT_GENERATION,
)

_BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_DEFAULT_COUNT = 5
_BRAVE_MAX_COUNT = 20
_BRAVE_TIMEOUT_S = 15.0


class IntelligenceAgentExecutor(_AgentExecutorBase):
    """A2A executor that runs the autonomous intelligence agent loop.

    Parameters
    ----------
    directory:
        Optional directory client used by ``discover_agents`` /
        ``query_peer_agent``. Tests inject a fake ADS-backed
        ``DirectoryClient``; production wires a ``_DirctlDirectory``
        pointing at the real ADS server.
    transport:
        Optional ``v2_agent_network.services.a2a.transport.A2ATransport``
        used to call discovered peers. Tests inject one wired to in-process
        mocks; production uses ``HTTPA2ATransport()`` per call.
    completion_fn:
        Optional async callable matching ``litellm.acompletion``. Tests
        inject a stub to drive the LLM loop deterministically.
    brave_api_key:
        Brave Search API key. Falls back to ``BRAVE_API_KEY`` env var.
        When neither is set, ``web_search`` returns an explicit
        ``not_configured`` error rather than failing the whole task.
    brave_http_client:
        Optional ``httpx.AsyncClient`` used for Brave Search. Tests inject
        one bound to an ``httpx.MockTransport`` so they can return canned
        results.
    """

    def __init__(
        self,
        directory: Any | None = None,
        transport: Any | None = None,
        completion_fn: Any | None = None,
        agent_step_limit: int = 8,
        agent_time_limit_s: float = 600.0,
        brave_api_key: str | None = None,
        brave_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if A2A_IMPORT_ERROR is not None:
            raise RuntimeError(
                "The a2a-sdk package is required for the intelligence agent. "
                "Install AgenticNetwork dependencies first."
            ) from A2A_IMPORT_ERROR
        self._directory = directory
        self._transport = transport
        self._completion_fn = completion_fn
        self._agent_step_limit = agent_step_limit
        self._agent_time_limit_s = agent_time_limit_s
        self._brave_api_key = brave_api_key or os.getenv("BRAVE_API_KEY")
        self._brave_http_client = brave_http_client

    async def execute(
        self, context: A2ARequestContext, event_queue: A2AEventQueue
    ) -> None:
        from agent_loop import AgentTrace, run_agent_loop

        updater = _task_updater(event_queue, context.task_id, context.context_id)
        prompt = _user_text(context)
        prompt_preview = prompt.replace("\n", " ")[:120]
        logger.info(
            "request received: task_id=%s context_id=%s prompt=%r",
            context.task_id, context.context_id, prompt_preview,
        )

        await updater.start_work()
        request_started = time.monotonic()

        peer_calls: list[dict[str, Any]] = []
        web_search_calls: list[dict[str, Any]] = []
        delivery_results: list[dict[str, Any]] = []
        discovered_cache: dict[str, list[dict[str, Any]]] = {}

        tool_handlers = self._build_tool_handlers(
            context_id=context.context_id,
            peer_calls=peer_calls,
            web_search_calls=web_search_calls,
            delivery_results=delivery_results,
            discovered_cache=discovered_cache,
        )

        model_name = (
            os.getenv("INTEL_AGENT_PLANNER_MODEL")
            or os.getenv("INTEL_AGENT_MODEL")
            or "openai/gpt-5-mini"
        )

        try:
            trace: AgentTrace = await run_agent_loop(
                user_prompt=prompt,
                model=model_name,
                tool_handlers=tool_handlers,
                completion_fn=self._completion_fn,
                step_limit=self._agent_step_limit,
                time_limit_s=self._agent_time_limit_s,
            )
        except Exception as exc:
            logger.exception(
                "agent loop crashed: task_id=%s elapsed=%.2fs",
                context.task_id, time.monotonic() - request_started,
            )
            await _emit_failure(updater, context, exc)
            return

        logger.info(
            "agent loop finished: task_id=%s exit=%s steps=%d tools=%d "
            "elapsed=%.2fs peer_calls=%d web_searches=%d deliveries=%d",
            context.task_id,
            trace.exit_reason, trace.n_steps, len(trace.tool_calls),
            trace.elapsed, len(peer_calls), len(web_search_calls),
            len(delivery_results),
        )

        output: dict[str, Any] = {
            "user_prompt": prompt,
            "context_id": context.context_id,
            "task_id": context.task_id,
            "peer_calls": peer_calls,
            "web_search_calls": web_search_calls,
            "delivery_results": delivery_results,
            "agent": {
                "final_message": trace.final_message,
                "exit_reason": trace.exit_reason,
                "n_steps": trace.n_steps,
                "elapsed_s": trace.elapsed,
                "tool_calls": [
                    {
                        "name": tc.name,
                        "args_keys": sorted(tc.args.keys()),
                        "result_keys": sorted((tc.result or {}).keys()) if tc.result else [],
                        "error": tc.error,
                    }
                    for tc in trace.tool_calls
                ],
            },
        }

        await updater.add_artifact(
            [_data_part(output)],
            artifact_id="intelligence-agent-result",
            name="intelligence_agent_result",
            metadata={"content_type": "application/json"},
        )

        summary = trace.final_message or _autosummary(
            peer_calls, web_search_calls, trace.exit_reason
        )
        message = updater.new_agent_message(
            [_text_part(summary)],
            metadata={"structured_output": output},
        )
        await updater.complete(message=message)
        logger.info(
            "request done: task_id=%s state=completed total_elapsed=%.2fs",
            context.task_id, time.monotonic() - request_started,
        )

    def _build_tool_handlers(
        self,
        *,
        context_id: str,
        peer_calls: list[dict[str, Any]],
        web_search_calls: list[dict[str, Any]],
        delivery_results: list[dict[str, Any]],
        discovered_cache: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:

        async def _discover(skill_query: str) -> list[Any]:
            if skill_query in discovered_cache:
                # Return objects rebuilt from the cached dicts (we only need
                # ``agent_id``/``name``/``endpoint`` downstream).
                from types import SimpleNamespace
                return [SimpleNamespace(**entry) for entry in discovered_cache[skill_query]]
            client = self._directory or _build_directory_client({})
            matches = await asyncio.to_thread(client.search_agents, skill=skill_query)
            cached: list[dict[str, Any]] = []
            for match in matches or []:
                cached.append({
                    "agent_id": getattr(match, "agent_id", None),
                    "name": getattr(match, "name", None),
                    "endpoint": getattr(match, "endpoint", None),
                    "description": getattr(match, "description", None),
                    "transport": getattr(match, "transport", "http"),
                    "source_record_id": getattr(match, "source_record_id", None),
                    "source_card_url": getattr(
                        match, "source_card_url", getattr(match, "endpoint", None)
                    ),
                    "skills": [
                        getattr(s, "name", None) or getattr(s, "id", None) or str(s)
                        for s in (getattr(match, "skills", None) or [])
                    ],
                })
            discovered_cache[skill_query] = cached
            return matches or []

        async def tool_discover_agents(args: Any) -> dict[str, Any]:
            skill_query = str((args or {}).get("skill_query") or "").strip()
            if not skill_query:
                return {"error": "discover_agents requires 'skill_query'"}
            try:
                matches = await _discover(skill_query)
            except Exception as exc:
                return {
                    "error": f"directory_unavailable: {type(exc).__name__}: {exc}",
                    "skill_query": skill_query,
                }
            cached = discovered_cache.get(skill_query) or []
            logger.info(
                "discover_agents: skill_query=%s matched=%d", skill_query, len(cached),
            )
            return {"skill_query": skill_query, "agents": cached}

        async def tool_query_peer(args: Any) -> dict[str, Any]:
            args = args or {}
            skill_query = str(args.get("skill_query") or "").strip()
            prompt = str(args.get("prompt") or "").strip()
            requested_agent_id = args.get("agent_id")
            if not skill_query:
                return {"error": "query_peer_agent requires 'skill_query'"}
            if not prompt:
                return {"error": "query_peer_agent requires 'prompt'"}
            try:
                matches = await _discover(skill_query)
            except Exception as exc:
                result = {
                    "status": "directory_unavailable",
                    "skill_query": skill_query,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                peer_calls.append(result)
                return result
            if not matches:
                result = {
                    "status": "no_agent_found",
                    "skill_query": skill_query,
                }
                peer_calls.append(result)
                return result
            target = None
            if requested_agent_id:
                for candidate in matches:
                    if getattr(candidate, "agent_id", None) == requested_agent_id:
                        target = candidate
                        break
            if target is None:
                target = matches[0]

            try:
                result = await _send_a2a_request(
                    agent=target,
                    skill=skill_query,
                    prompt=prompt,
                    context_id=context_id,
                    transport=self._transport,
                )
            except Exception as exc:
                result = {
                    "status": "send_failed",
                    "agent_id": getattr(target, "agent_id", None),
                    "agent_endpoint": getattr(target, "endpoint", None),
                    "skill_query": skill_query,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                peer_calls.append(result)
                return result
            state = result.get("state")
            status = "ok" if state == "completed" and not result.get("error") else state or "unknown"
            outcome = {
                "status": status,
                "agent_id": getattr(target, "agent_id", None),
                "agent_name": getattr(target, "name", None),
                "agent_endpoint": getattr(target, "endpoint", None),
                "skill_query": skill_query,
                "prompt": prompt,
                "reply_text": result.get("summary"),
                "structured_output": result.get("structured_output"),
                "send_state": state,
                "error": result.get("error"),
            }
            peer_calls.append(outcome)
            logger.info(
                "query_peer_agent: skill=%s agent=%s status=%s",
                skill_query, outcome["agent_id"], status,
            )
            return outcome

        async def tool_web_search(args: Any) -> dict[str, Any]:
            args = args or {}
            query = str(args.get("query") or "").strip()
            count = int(args.get("count") or _BRAVE_DEFAULT_COUNT)
            count = max(1, min(_BRAVE_MAX_COUNT, count))
            if not query:
                return {"error": "web_search requires 'query'"}
            if not self._brave_api_key:
                outcome = {
                    "status": "not_configured",
                    "query": query,
                    "error": (
                        "BRAVE_API_KEY is not set; web_search is unavailable. "
                        "The agent should rely on peer agents for context."
                    ),
                }
                web_search_calls.append(outcome)
                return outcome
            try:
                results = await _brave_search(
                    query=query,
                    count=count,
                    api_key=self._brave_api_key,
                    http_client=self._brave_http_client,
                )
            except Exception as exc:
                outcome = {
                    "status": "search_failed",
                    "query": query,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                web_search_calls.append(outcome)
                return outcome
            outcome = {
                "status": "ok",
                "query": query,
                "count": len(results),
                "results": results,
            }
            web_search_calls.append(outcome)
            logger.info("web_search: query=%r results=%d", query[:60], len(results))
            return outcome

        async def tool_group_email(args: Any) -> dict[str, Any]:
            args = args or {}
            subject = str(args.get("subject") or "").strip()
            body = str(args.get("body") or "").strip()
            importance = str(args.get("importance") or "normal").strip().lower()
            if importance not in {"normal", "high"}:
                importance = "normal"
            if not subject:
                return {"error": "send_group_email_via_directory requires non-empty 'subject'"}
            if not body:
                return {"error": "send_group_email_via_directory requires non-empty 'body'"}

            skill_query = "email/group_send"
            try:
                matches = await _discover(skill_query)
            except Exception as exc:
                outcome = {
                    "status": "directory_unavailable",
                    "kind": "group_email",
                    "skill_query": skill_query,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                delivery_results.append(outcome)
                return outcome
            if not matches:
                outcome = {
                    "status": "no_agent_found",
                    "kind": "group_email",
                    "skill_query": skill_query,
                }
                delivery_results.append(outcome)
                return outcome
            target = matches[0]

            try:
                send_result = await _send_a2a_group_email_request(
                    agent=target,
                    subject=subject,
                    body=body,
                    importance=importance,
                    context_id=context_id,
                    transport=self._transport,
                )
            except Exception as exc:
                outcome = {
                    "status": "send_failed",
                    "kind": "group_email",
                    "agent_id": getattr(target, "agent_id", None),
                    "agent_endpoint": getattr(target, "endpoint", None),
                    "skill_query": skill_query,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                delivery_results.append(outcome)
                return outcome

            state = send_result.get("state")
            if state == "completed" and not send_result.get("error"):
                status = "sent"
            elif state == "input_required" and not send_result.get("error"):
                status = "pending_confirmation"
            else:
                status = "send_rejected"
            outcome = {
                "status": status,
                "kind": "group_email",
                "agent_id": getattr(target, "agent_id", None),
                "agent_name": getattr(target, "name", None),
                "agent_endpoint": getattr(target, "endpoint", None),
                "skill_query": skill_query,
                "subject": subject,
                "importance": importance,
                "send_state": state,
                "summary": send_result.get("summary"),
                "error": send_result.get("error"),
            }
            delivery_results.append(outcome)
            logger.info(
                "send_group_email_via_directory: status=%s agent=%s subject=%r importance=%s",
                status, outcome["agent_id"], subject[:80], importance,
            )
            return outcome

        return {
            "discover_agents": tool_discover_agents,
            "query_peer_agent": tool_query_peer,
            "web_search": tool_web_search,
            "send_group_email_via_directory": tool_group_email,
        }

    async def cancel(
        self, context: A2ARequestContext, event_queue: A2AEventQueue
    ) -> None:
        updater = _task_updater(event_queue, context.task_id, context.context_id)
        await updater.reject()


def load_intel_a2a_agent_card(
    card_path: Path | str = DEFAULT_INTEL_A2A_CARD,
) -> A2AAgentCard:
    _require_a2a()
    assert _AgentCard is not None
    with Path(card_path).open("r", encoding="utf-8") as fh:
        return _AgentCard.model_validate(json.load(fh))


def create_intel_a2a_app(
    agent_card: A2AAgentCard,
    executor: IntelligenceAgentExecutor | None = None,
) -> Any:
    _require_a2a()
    assert _A2AStarletteApplication is not None
    assert _DefaultRequestHandler is not None
    assert _InMemoryTaskStore is not None
    request_handler = _DefaultRequestHandler(
        agent_executor=executor or IntelligenceAgentExecutor(),
        task_store=_InMemoryTaskStore(),
    )
    return _A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()


def _require_a2a() -> None:
    if A2A_IMPORT_ERROR is not None:
        raise RuntimeError(
            "A2A server mode requires the a2a-sdk, starlette, and uvicorn packages. "
            "Install the AgenticNetwork dependencies before running serve-a2a."
        ) from A2A_IMPORT_ERROR


def _task_updater(
    event_queue: A2AEventQueue,
    task_id: str,
    context_id: str,
) -> A2ATaskUpdater:
    _require_a2a()
    assert _TaskUpdater is not None
    return _TaskUpdater(event_queue, task_id, context_id)


def _autosummary(
    peer_calls: list[dict[str, Any]],
    web_search_calls: list[dict[str, Any]],
    exit_reason: str,
) -> str:
    parts: list[str] = []
    if peer_calls:
        ok = sum(1 for p in peer_calls if p.get("status") == "ok")
        parts.append(f"peer calls: {len(peer_calls)} ({ok} ok)")
    if web_search_calls:
        ok = sum(1 for w in web_search_calls if w.get("status") == "ok")
        parts.append(f"web searches: {len(web_search_calls)} ({ok} ok)")
    if not parts:
        parts.append("no peer or web data collected")
    parts.append(f"exit_reason={exit_reason}")
    return "; ".join(parts)


def _data_part(data: dict[str, Any]) -> Any:
    _require_a2a()
    assert _DataPart is not None
    assert _Part is not None
    return _Part(root=_DataPart(data=data))


def _text_part(text: str) -> Any:
    _require_a2a()
    assert _Part is not None
    assert _TextPart is not None
    return _Part(root=_TextPart(text=text))


def _user_text(context: A2ARequestContext) -> str:
    try:
        return str(context.get_user_input())
    except Exception:
        return ""


async def _emit_failure(
    updater: A2ATaskUpdater,
    context: A2ARequestContext,
    error: BaseException,
) -> None:
    payload = {
        "context_id": context.context_id,
        "task_id": context.task_id,
        "error": f"{type(error).__name__}: {error}",
    }
    await updater.add_artifact(
        [_data_part(payload)],
        artifact_id="intelligence-agent-error",
        name="intelligence_agent_error",
        metadata={"content_type": "application/json", "error": True},
    )
    await updater.failed(
        message=updater.new_agent_message(
            [_text_part(payload["error"])],
            metadata={"structured_output": payload},
        )
    )


# ---------------------------------------------------------------------------
# A2A peer call (generic — not email-specific)
# ---------------------------------------------------------------------------


async def _send_a2a_request(
    *,
    agent: Any,
    skill: str,
    prompt: str,
    context_id: str,
    transport: Any | None,
) -> dict[str, Any]:
    """Send a natural-language A2A ``message/send`` to a discovered peer.

    Returns ``{state, summary, structured_output, message_id, error}``. The
    peer's text reply (last message's first text part) is in ``summary``;
    its structured artifact (if any) is in ``structured_output``.
    """
    from v2_agent_network.services.a2a.client import A2AClient
    from v2_agent_network.services.a2a.transport import HTTPA2ATransport
    from v2_agent_network.services.common.models import (
        A2AMessage,
        A2APart,
        A2ATaskRequest,
    )

    request = A2ATaskRequest(
        task_id=str(uuid.uuid4()),
        context_id=context_id,
        target_agent_id=getattr(agent, "agent_id", None) or "unknown",
        skill=skill,
        user_goal=prompt[:200],
        messages=[
            A2AMessage(
                role="user",
                parts=[A2APart(kind="text", text=prompt)],
            )
        ],
        metadata={"skill": skill, "requested_skill": skill},
    )

    if transport is None:
        transport = HTTPA2ATransport()
    client = A2AClient(transport)
    result = await client.send_task(agent, request)
    state = result.state.value if hasattr(result.state, "value") else str(result.state)

    summary = None
    if result.messages:
        last = result.messages[-1]
        for part in last.parts:
            if part.kind == "text" and part.text:
                summary = part.text
                break

    structured = result.structured_output or None
    error = None
    if isinstance(structured, dict):
        error = structured.get("error")

    return {
        "state": state,
        "message_id": (structured or {}).get("message_id") if isinstance(structured, dict) else None,
        "structured_output": structured,
        "summary": summary,
        "error": error,
    }


async def _send_a2a_group_email_request(
    *,
    agent: Any,
    subject: str,
    body: str,
    importance: str,
    context_id: str,
    transport: Any | None,
) -> dict[str, Any]:
    """Send an A2A ``email/group_send`` request to a discovered email agent.

    The email agent owns the recipient list (``notice_list.txt``); we only
    pass subject + body + an optional importance flag. Sent confirmed so the
    email agent doesn't bounce back an ``input_required`` for routine
    intelligence-side broadcasts.
    """
    from v2_agent_network.services.a2a.client import A2AClient
    from v2_agent_network.services.a2a.transport import HTTPA2ATransport
    from v2_agent_network.services.common.models import (
        A2AMessage,
        A2APart,
        A2ATaskRequest,
    )

    skill = "email/group_send"
    payload: dict[str, Any] = {
        "subject": subject,
        "body": body,
        "importance": importance,
    }
    metadata: dict[str, Any] = {
        "skill": skill,
        "requested_skill": skill,
        "confirmation_status": "confirmed",
        "confirmed": True,
        **payload,
    }
    text_intro = f"[group_send importance={importance}] {subject}\n\n{body}"

    request = A2ATaskRequest(
        task_id=str(uuid.uuid4()),
        context_id=context_id,
        target_agent_id=getattr(agent, "agent_id", None) or "email-agent",
        skill=skill,
        user_goal=f"Broadcast intelligence brief: {subject[:160]}",
        messages=[
            A2AMessage(
                role="user",
                parts=[
                    A2APart(kind="text", text=text_intro),
                    A2APart(
                        kind="data",
                        content_type="application/json",
                        data=payload,
                    ),
                ],
            )
        ],
        metadata=metadata,
    )

    if transport is None:
        transport = HTTPA2ATransport()
    client = A2AClient(transport)
    result = await client.send_task(agent, request)
    state = result.state.value if hasattr(result.state, "value") else str(result.state)

    summary = None
    if result.messages:
        last = result.messages[-1]
        for part in last.parts:
            if part.kind == "text" and part.text:
                summary = part.text
                break
    structured = result.structured_output if isinstance(result.structured_output, dict) else None
    error = (structured or {}).get("error") if structured else None

    return {
        "state": state,
        "message_id": (structured or {}).get("message_id") if structured else None,
        "structured_output": structured,
        "summary": summary,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Brave Search
# ---------------------------------------------------------------------------


async def _brave_search(
    *,
    query: str,
    count: int,
    api_key: str,
    http_client: httpx.AsyncClient | None,
) -> list[dict[str, Any]]:
    """Call Brave Search and normalize the results.

    Returns a list of ``{title, url, snippet, age}`` dicts. Raises if the
    HTTP call fails or the body isn't a recognizable Brave response.
    """
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": count}

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=_BRAVE_TIMEOUT_S)
    try:
        response = await client.get(
            _BRAVE_SEARCH_ENDPOINT, headers=headers, params=params
        )
    finally:
        if owns_client:
            await client.aclose()

    response.raise_for_status()
    body = response.json()
    web = (body.get("web") or {}).get("results") or []
    results: list[dict[str, Any]] = []
    for entry in web[:count]:
        if not isinstance(entry, dict):
            continue
        results.append(
            {
                "title": entry.get("title"),
                "url": entry.get("url"),
                "snippet": entry.get("description") or entry.get("snippet"),
                "age": entry.get("age"),
            }
        )
    return results


# ---------------------------------------------------------------------------
# dirctl-backed directory client (vendored from the coding agent)
# ---------------------------------------------------------------------------


class _DirctlError(RuntimeError):
    """Raised when a `dirctl` invocation exits non-zero or returns unparseable output."""


class _DirctlDirectory:
    """Talk to a real ADS via the ``dirctl`` CLI.

    Identical in shape to the coding agent's helper. The Python
    ``agntcy_dir`` SDK is broken in this checkout (its generated protobuf
    needs a runtime newer than the pinned ``protobuf``), so we shell out
    to ``dirctl`` instead.
    """

    def __init__(
        self,
        server_addr: str,
        dirctl_path: str = "dirctl",
        timeout: float = 30.0,
        extra_args: Sequence[str] = (),
    ) -> None:
        self.server_addr = server_addr
        self.dirctl_path = dirctl_path
        self.timeout = timeout
        self._extra_args = list(extra_args)

    def _base_cmd(self) -> list[str]:
        return [self.dirctl_path, "--server-addr", self.server_addr, *self._extra_args]

    def _run(self, args: Sequence[str]) -> str:
        cmd = self._base_cmd() + list(args)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise _DirctlError(
                f"`{self.dirctl_path}` not found on PATH. Install dirctl or "
                "pass --dirctl-path."
            ) from exc
        if result.returncode != 0:
            raise _DirctlError(
                f"`{' '.join(cmd)}` exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout

    def list_records(self) -> list[dict[str, Any]]:
        out = self._run(["routing", "list", "--output", "json"])
        if not out.strip():
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise _DirctlError(f"Failed to parse `routing list` JSON: {exc}") from exc
        return data if isinstance(data, list) else []

    def pull_record(self, cid: str) -> dict[str, Any]:
        out = self._run(["pull", cid, "--output", "json"])
        if not out.strip():
            raise _DirctlError(f"`pull {cid}` returned no output.")
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise _DirctlError(f"Failed to parse `pull` JSON for {cid}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def search_agents(self, skill: str) -> list[Any]:
        candidates = self.list_records()
        results: list[Any] = []
        for entry in candidates:
            cid = (entry.get("record_ref") or {}).get("cid")
            labels = entry.get("labels") or []
            if not cid or "/modules/integration/a2a" not in labels:
                continue
            try:
                record = self.pull_record(cid)
            except _DirctlError:
                continue
            if not _record_supports_custom_skill(record, skill):
                continue
            agent = _record_to_discovered_agent(record, cid)
            if agent is not None:
                results.append(agent)
        return results

    def push(self, record_path: Path) -> str:
        out = self._run(["push", str(record_path), "--output", "raw"])
        cid = out.strip()
        if not cid:
            raise _DirctlError("`dirctl push` produced no CID on stdout.")
        return cid

    def publish(self, cid: str) -> None:
        self._run(["routing", "publish", cid])

    def unpublish(self, cid: str) -> None:
        self._run(["routing", "unpublish", cid])


def _record_supports_custom_skill(record: dict[str, Any], skill: str) -> bool:
    annotations = record.get("annotations") or {}
    tags = annotations.get("agentic_network.custom_skill_tags") or ""
    if any(tag.strip() == skill for tag in tags.split(",")):
        return True
    for module in record.get("modules") or []:
        data = (module or {}).get("data") or {}
        card_data = data.get("card_data") or {}
        for entry in card_data.get("skills") or []:
            if (entry or {}).get("id") == skill:
                return True
    return False


def _record_to_discovered_agent(record: dict[str, Any], cid: str) -> Any:
    from v2_agent_network.services.common.models import DiscoveredAgent

    annotations = record.get("annotations") or {}
    a2a_url = annotations.get("a2a.url")
    if not a2a_url:
        for locator in record.get("locators") or []:
            la = (locator or {}).get("annotations") or {}
            if la.get("a2a.url"):
                a2a_url = la["a2a.url"]
                break
    if not a2a_url:
        for module in record.get("modules") or []:
            data = (module or {}).get("data") or {}
            card_data = data.get("card_data") or {}
            if card_data.get("url"):
                a2a_url = card_data["url"]
                break
    if not a2a_url:
        return None
    agent_id = (
        annotations.get("agentic_network.agent_id")
        or str(record.get("name") or "unknown").rstrip("/").split("/")[-1]
    )
    name = annotations.get("agentic_network.display_name") or str(
        record.get("name") or agent_id
    )
    organization = annotations.get("agentic_network.organization") or "unknown"
    description = str(record.get("description") or "")
    return DiscoveredAgent(
        agent_id=agent_id,
        name=name,
        organization=organization,
        description=description,
        endpoint=a2a_url,
        transport="http",
        source_record_id=cid,
        source_card_url=a2a_url,
    )


def _build_directory_client(delivery: dict[str, Any]) -> Any:
    """Pick a directory backend.

    Order:
    1. ``delivery.fake_ads`` or ``AGENT_NETWORK_FAKE_ADS`` → in-memory fake.
    2. ``INTEL_AGENT_DIRECTORY_URL`` / ``ADS_URL`` → ``_DirctlDirectory``.
    3. Otherwise raise.
    """
    fake_ads_value = delivery.get("fake_ads")
    if fake_ads_value is None:
        fake_ads_value = os.getenv(
            "AGENT_NETWORK_FAKE_ADS", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
    if fake_ads_value:
        from v2_agent_network.services.directory.client import DirectoryClient
        return DirectoryClient(fake_ads=True)

    server_addr = (
        delivery.get("directory_url")
        or os.getenv("INTEL_AGENT_DIRECTORY_URL")
        or os.getenv("ADS_URL")
    )
    if server_addr:
        return _DirctlDirectory(server_addr=server_addr)

    raise RuntimeError(
        "No directory configured. Set INTEL_AGENT_DIRECTORY_URL or ADS_URL — "
        "or pass fake_ads=true."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Autonomous Intelligence Agent A2A wrapper. Speaks A2A on a port "
            "the AgenticNetwork PI agent can reach; runs a tool-calling LLM "
            "loop with discover_agents / query_peer_agent / web_search to "
            "synthesize situational awareness, threat assessment, and a "
            "recommended course of action."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    a2a_parser = subparsers.add_parser(
        "serve-a2a",
        help="Run the intelligence agent as an A2A HTTP server.",
    )
    a2a_parser.add_argument(
        "--host",
        default=os.getenv("INTEL_AGENT_A2A_HOST", "0.0.0.0"),
        help="Host interface to bind. Default: 0.0.0.0.",
    )
    a2a_parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("INTEL_AGENT_A2A_PORT", "9105")),
        help="Port for the A2A HTTP server. Default: 9105 (matches the card).",
    )
    a2a_parser.add_argument(
        "--card",
        type=Path,
        default=DEFAULT_INTEL_A2A_CARD,
        help="A2A Agent Card JSON file.",
    )
    a2a_parser.add_argument(
        "--allow-missing-model",
        action="store_true",
        help=(
            "Skip the upfront INTEL_AGENT_MODEL check. Useful when running "
            "tests against an injected fake completion. Default: off."
        ),
    )
    a2a_parser.add_argument(
        "--directory-server",
        default=os.getenv("INTEL_AGENT_DIRECTORY_URL") or os.getenv("ADS_URL"),
        help=(
            "ADS server address for `dirctl` (e.g. 10.229.117.37:8888). When "
            "set, the intelligence agent self-publishes on startup, "
            "unpublishes on exit, and uses this directory for peer discovery. "
            "Defaults to INTEL_AGENT_DIRECTORY_URL or ADS_URL."
        ),
    )
    a2a_parser.add_argument(
        "--dirctl-path",
        default="dirctl",
        help="Path to the dirctl binary. Default: search PATH.",
    )
    a2a_parser.add_argument(
        "--no-publish",
        action="store_true",
        help=(
            "Do not push/publish this agent's record to ADS even if "
            "--directory-server is set. Discovery still uses dirctl."
        ),
    )
    a2a_parser.add_argument(
        "--log-level",
        default=os.getenv("INTEL_AGENT_LOG_LEVEL", "info"),
        choices=["debug", "info", "warning", "error", "critical"],
        help="Log level. Default: info (env: INTEL_AGENT_LOG_LEVEL).",
    )

    run_parser = subparsers.add_parser(
        "run-once",
        help="Run a single intelligence request from the CLI (no A2A round-trip).",
    )
    run_parser.add_argument("--prompt", required=True, help="User prompt.")
    run_parser.add_argument(
        "--directory-server",
        default=os.getenv("INTEL_AGENT_DIRECTORY_URL") or os.getenv("ADS_URL"),
        help="ADS server address for dirctl-based discovery.",
    )
    run_parser.add_argument(
        "--fake-ads",
        action="store_true",
        help="Use the in-memory fake directory (single process, debug only).",
    )

    subparsers.add_parser(
        "tools",
        help="Print every skill the executor advertises.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "tools":
        for skill in _INTEL_SKILLS:
            print(skill)
        return 0

    if args.command == "run-once":
        return _cli_run_once(args)

    if args.command == "serve-a2a":
        log_level = args.log_level.upper()
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        logging.getLogger("a2a").setLevel(log_level)

        if not args.allow_missing_model:
            _check_model_configured()

        _require_a2a()
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError(
                "The uvicorn package is required for serve-a2a."
            ) from exc

        card = load_intel_a2a_agent_card(args.card)

        directory = None
        if args.directory_server:
            directory = _DirctlDirectory(
                server_addr=args.directory_server,
                dirctl_path=args.dirctl_path,
            )

        executor = IntelligenceAgentExecutor(directory=directory)
        app = create_intel_a2a_app(card, executor=executor)

        published_cid: str | None = None
        if args.directory_server and not args.no_publish:
            try:
                published_cid = _publish_self_to_ads(
                    args.card,
                    args.directory_server,
                    dirctl_path=args.dirctl_path,
                )
                logger.info(
                    "published to ADS: server=%s cid=%s",
                    args.directory_server, published_cid,
                )
            except _DirctlError as exc:
                logger.error("failed to publish to ADS: %s", exc)
                return 3

        logger.info(
            "serving A2A: bind=http://%s:%s/ card_url=%s brave=%s",
            args.host, args.port, card.url,
            "configured" if os.getenv("BRAVE_API_KEY") else "missing (web_search disabled)",
        )
        try:
            uvicorn.run(
                app,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
            )
        finally:
            if published_cid is not None:
                try:
                    _unpublish_self_from_ads(
                        args.directory_server,
                        published_cid,
                        dirctl_path=args.dirctl_path,
                    )
                    logger.info(
                        "unpublished from ADS: server=%s cid=%s",
                        args.directory_server, published_cid,
                    )
                except _DirctlError as exc:
                    logger.error(
                        "failed to unpublish cid=%s: %s", published_cid, exc,
                    )
        return 0

    return 2


def _publish_self_to_ads(
    card_path: Path, server_addr: str, *, dirctl_path: str = "dirctl"
) -> str:
    from _ads_record import agent_card_to_ads_record, load_agent_card

    card = load_agent_card(card_path)
    record = agent_card_to_ads_record(card)
    fh = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".record.json", delete=False
    )
    record_path = Path(fh.name)
    try:
        json.dump(record, fh, indent=2)
        fh.close()
        directory = _DirctlDirectory(server_addr=server_addr, dirctl_path=dirctl_path)
        cid = directory.push(record_path)
        directory.publish(cid)
        return cid
    finally:
        try:
            record_path.unlink()
        except FileNotFoundError:
            pass


def _unpublish_self_from_ads(
    server_addr: str, cid: str, *, dirctl_path: str = "dirctl"
) -> None:
    directory = _DirctlDirectory(server_addr=server_addr, dirctl_path=dirctl_path)
    directory.unpublish(cid)


def _cli_run_once(args: argparse.Namespace) -> int:
    """Run the agent loop directly without the A2A wrapper.

    This is mostly a debugging shortcut. It still needs an LLM provider
    (e.g. ``OPENAI_API_KEY`` and ``INTEL_AGENT_MODEL``) and, if the LLM
    actually calls peer tools, a reachable directory.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    directory: Any = None
    if args.fake_ads:
        directory = _build_directory_client({"fake_ads": True})
    elif args.directory_server:
        directory = _DirctlDirectory(server_addr=args.directory_server)

    executor = IntelligenceAgentExecutor(directory=directory)
    handlers = executor._build_tool_handlers(
        context_id=str(uuid.uuid4()),
        peer_calls=[],
        web_search_calls=[],
        delivery_results=[],
        discovered_cache={},
    )
    from agent_loop import run_agent_loop

    model_name = (
        os.getenv("INTEL_AGENT_PLANNER_MODEL")
        or os.getenv("INTEL_AGENT_MODEL")
        or "openai/gpt-5-mini"
    )

    async def _go() -> None:
        trace = await run_agent_loop(
            user_prompt=args.prompt,
            model=model_name,
            tool_handlers=handlers,
        )
        sys.stdout.write(
            json.dumps(
                {
                    "final_message": trace.final_message,
                    "exit_reason": trace.exit_reason,
                    "n_steps": trace.n_steps,
                    "elapsed_s": trace.elapsed,
                    "tool_calls": [
                        {"name": tc.name, "args": tc.args, "error": tc.error}
                        for tc in trace.tool_calls
                    ],
                },
                indent=2,
                default=str,
            )
            + "\n"
        )

    asyncio.run(_go())
    return 0


def _check_model_configured() -> None:
    if os.getenv("INTEL_AGENT_MODEL") or os.getenv("INTEL_AGENT_PLANNER_MODEL"):
        return
    sys.stderr.write(
        "[intelligence_agent] INTEL_AGENT_MODEL is not set. The default planner "
        "model is openai/gpt-5-mini, which requires OPENAI_API_KEY. Pass "
        "--allow-missing-model to bypass this check.\n"
    )
    if not os.getenv("OPENAI_API_KEY") and not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
