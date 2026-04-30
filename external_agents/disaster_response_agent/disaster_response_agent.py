#!/usr/bin/env python3
"""Standalone disaster response planning agent with optional A2A serving."""

from __future__ import annotations

import argparse
import atexit
import html as html_lib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9104
DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_ADS_SERVER_ADDR = "10.229.117.37:8888"
DEFAULT_ADS_BACKEND = "cli"
DEFAULT_AGENT_URL = "http://10.229.117.223:9104/"
DEFAULT_CARD_PATH = Path(__file__).resolve().parent / "disaster_response_agent.card.json"
DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS = 30
DISASTER_STATUS_LOG_FALSE_VALUES = {"0", "false", "no", "off"}
LOG_REDACT_KEY_PARTS = {
    "authorization",
    "client_secret",
    "credential",
    "password",
    "refresh_token",
    "token",
    "api_key",
}
DEFAULT_LOG_VALUE_CHARS = 900
OFFICIAL_ADS_ROUTING_SKILLS = [
    {
        "name": "natural_language_processing/natural_language_generation/summarization",
        "id": 10202,
    },
    {
        "name": "natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search",
        "id": 10306,
    },
]


def _disaster_status(message: str, **details: Any) -> None:
    if os.getenv("DISASTER_AGENT_STATUS_LOGS", "1").strip().lower() in DISASTER_STATUS_LOG_FALSE_VALUES:
        return
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    rendered = " ".join(
        f"{key}={_log_render(value)}"
        for key, value in details.items()
        if value is not None and value != ""
    )
    suffix = f" {rendered}" if rendered else ""
    print(f"[disaster-agent] {timestamp} {message}{suffix}", file=sys.stderr, flush=True)


def _log_render(value: Any) -> str:
    sanitized = _log_sanitize(value)
    if isinstance(sanitized, str):
        rendered = sanitized.replace("\n", "\\n")
    else:
        rendered = json.dumps(sanitized, default=str, sort_keys=True)
    max_chars = int(os.getenv("DISASTER_AGENT_LOG_MAX_CHARS", str(DEFAULT_LOG_VALUE_CHARS)))
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "...[truncated]"
    return rendered


def _log_sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(part in lowered for part in LOG_REDACT_KEY_PARTS):
                sanitized[key_text] = "[redacted]"
            else:
                sanitized[key_text] = _log_sanitize(item, depth=depth + 1)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        sanitized_items = [_log_sanitize(item, depth=depth + 1) for item in items[:20]]
        if len(items) > 20:
            sanitized_items.append(f"... {len(items) - 20} more")
        return sanitized_items
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class PublishedRecord:
    record_id: str
    agent_id: str
    backend: str
    backend_ref: str | None = None


DISASTER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "discover_ads_agents",
        "description": (
            "Search the real ADS directory for registered agents that may help "
            "with the disaster response task, such as email, weather, geospatial, "
            "shelter, transport, or communication agents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "Optional skill tag or official OASF skill name. Use empty string to list agents.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional text filter over names, descriptions, organizations, and skill text.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum agents to return.",
                },
                "include_self": {
                    "type": "boolean",
                    "description": "Whether to include this Disaster Response Agent in results.",
                },
            },
            "required": ["skill", "query", "limit", "include_self"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "send_a2a_request",
        "description": (
            "Send a request to another ADS-discovered A2A agent. Use discover_ads_agents "
            "first, then pass the returned cid, record_id, agent_id, name, or endpoint as agent_ref. "
            "Do not use this for weapons/offensive harm."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_ref": {
                    "type": "string",
                    "description": "Agent reference from ADS discovery: cid, record_id, agent_id, name, or A2A endpoint.",
                },
                "skill": {
                    "type": "string",
                    "description": "Target skill tag from the discovered agent card.",
                },
                "message": {
                    "type": "string",
                    "description": "Natural-language instruction to send to the remote agent.",
                },
                "data": {
                    "type": "object",
                    "description": "Optional structured JSON payload for the remote agent. Use an empty object unless a fixed schema is provided.",
                    "properties": {},
                    "additionalProperties": False,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "description": "A2A request timeout in seconds.",
                },
            },
            "required": ["agent_ref", "skill", "message", "data", "timeout_seconds"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def _disaster_system_prompt() -> str:
    return (
        "You are an AI disaster response planning agent. Generate practical, "
        "situation-specific emergency guidance from the user's description. "
        "Prioritize immediate life safety, official local instructions, evacuation "
        "or shelter decisions, communication, accessibility, supplies, and actions "
        "to avoid. Do not provide offensive violence tactics, weapon instructions, "
        "or advice to evade law enforcement. For armed threats, focus on getting "
        "away if safe, hiding/sheltering quietly if escape is unsafe, contacting "
        "emergency services when safe, and following responder commands. For medical "
        "issues, provide only basic first-aid orientation and advise emergency care "
        "for serious injury. Acknowledge uncertainty and tailor the plan to location, "
        "audience, severity, and resources when provided. You may use tools to discover "
        "ADS agents and ask another A2A agent for relevant help, such as weather, map, "
        "email/notice drafting, geospatial, shelter, logistics, or communications. "
        "Use discover_ads_agents before send_a2a_request. Do not invent agents or endpoints. "
        "Call the email agent to send a campus group notice only when the request clearly "
        "describes an active or imminent real incident on campus, or an incident that could "
        "affect campus, and include clear Subject: and Body: lines in that request. "
        "For notification email bodies, write mobile-friendly text: short paragraphs, "
        "short bullet items, and no hard line wrapping inside sentences. Put line breaks "
        "only between paragraphs, headings, and bullet items. "
        "If the user asks only for a draft, template, drill, hypothetical scenario, "
        "preparedness guidance, or response plan without a real current threat, do not "
        "send or request any notification email. If it is unclear whether the threat is "
        "real, treat the request as planning guidance and do not notify. "
        "Contact other ADS agents directly when their help is useful. "
        "Do not ask remote agents to perform unsafe or offensive actions. Return ONLY valid JSON with "
        "these keys: summary (string), risk_level (low|medium|high|critical|unknown), "
        "incident_types (array of strings), immediate_actions (array), shelter_guidance "
        "(array), evacuation_guidance (array), communications (array), "
        "medical_and_accessibility (array), supplies (array), avoid (array), "
        "follow_up_checks (array), disclaimer (string), response_text (string). "
        "The response_text should be a readable version of the same plan."
    )


def _response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)

    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if _object_get(item, "type") != "message":
            continue
        for content in _object_get(item, "content", []) or []:
            if _object_get(content, "type") in {"output_text", "text"}:
                text = _object_get(content, "text", "")
                if text:
                    parts.append(str(text))
    return "\n".join(parts).strip()


def _object_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _extract_function_calls(response: Any) -> list[Any]:
    return [
        item
        for item in (getattr(response, "output", []) or [])
        if _object_get(item, "type") == "function_call"
    ]


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                parsed = {}
        else:
            parsed = {}

    if isinstance(parsed, dict):
        return parsed
    return {}


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _format_plan(plan: dict[str, Any]) -> str:
    sections = [
        ("Summary", [plan.get("summary", "")]),
        ("Do Now", plan.get("immediate_actions", [])),
        ("Shelter Guidance", plan.get("shelter_guidance", [])),
        ("Evacuation Guidance", plan.get("evacuation_guidance", [])),
        ("Communications", plan.get("communications", [])),
        ("Medical And Accessibility", plan.get("medical_and_accessibility", [])),
        ("Supplies To Prioritize", plan.get("supplies", [])),
        ("Avoid", plan.get("avoid", [])),
        ("Follow-Up Checks", plan.get("follow_up_checks", [])),
        ("Important Note", [plan.get("disclaimer", "")]),
    ]
    lines: list[str] = []
    for title, items in sections:
        normalized_items = _as_string_list(items)
        if not normalized_items:
            continue
        lines.append(f"{title}:")
        for item in normalized_items:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).strip()


class DisasterResponseAgent:
    """OpenAI-backed emergency response planner for A2A and CLI use."""

    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("DISASTER_AGENT_MODEL", DEFAULT_MODEL)

    def build_response(
        self,
        description: str,
        *,
        location: str = "",
        audience: str = "",
        severity: str = "",
        resources: list[str] | None = None,
        ads_url: str | None = None,
    ) -> dict[str, Any]:
        description = description.strip()
        if not description:
            raise ValueError("A situation description is required.")

        resources = resources or []
        raw_text, pending_actions = self._call_model(
            {
                "description": description,
                "location": location,
                "audience": audience,
                "severity": severity,
                "resources": resources,
            }
        )
        plan = self._normalize_plan(_extract_json_object(raw_text))
        response_text = str(plan.pop("response_text", "") or "").strip()
        if not response_text:
            response_text = _format_plan(plan)
        campus_notice_result = None
        if (
            _should_notify_campus(
                description=description,
                location=location,
                audience=audience,
                plan=plan,
            )
            and not pending_actions
            and not _mentions_email_agent(response_text)
        ):
            _disaster_status(
                "Campus notice trigger matched",
                description=description,
                location=location,
                audience=audience,
                severity=severity,
            )
            campus_notice_result = _send_campus_group_notice(
                description=description,
                location=location,
                audience=audience,
                severity=severity,
                plan=plan,
                ads_url=ads_url,
            )
            if campus_notice_result is not None:
                plan["campus_notice"] = _campus_notice_result_summary(campus_notice_result)
                pending_actions.extend(
                    _pending_actions_from_tool_result(
                        campus_notice_result,
                        tool_name="send_a2a_request",
                    )
                )
                response_text = _append_campus_notice_result_to_text(
                    response_text,
                    campus_notice_result,
                )
        if pending_actions:
            plan["pending_actions"] = pending_actions
            plan["follow_up_checks"].extend(_pending_action_follow_up_lines(pending_actions))
            response_text = _append_pending_actions_to_text(response_text, pending_actions)

        return {
            "status": "completed",
            "model": self.model,
            "input": {
                "description": description,
                "location": location,
                "audience": audience,
                "severity": severity,
                "resources": resources,
                "ads_url": ads_url,
            },
            "response": plan,
            "response_text": response_text,
        }

    def _call_model(self, payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("Missing OPENAI_API_KEY for the Disaster Response AI agent.")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("The openai package is required for disaster response AI mode.") from exc

        _disaster_status("Model request started", model=self.model, payload=payload)
        client = OpenAI()
        response = client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": _disaster_system_prompt(),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, indent=2),
                },
            ],
            tools=DISASTER_TOOL_SCHEMAS,
            parallel_tool_calls=False,
        )
        pending_actions: list[dict[str, Any]] = []
        for _ in range(4):
            tool_calls = _extract_function_calls(response)
            if not tool_calls:
                break
            tool_outputs = []
            for call in tool_calls:
                name = _object_get(call, "name", "")
                call_id = _object_get(call, "call_id", "")
                raw_arguments = _object_get(call, "arguments", "{}")
                try:
                    arguments = json.loads(raw_arguments or "{}")
                    _disaster_status(
                        "Agent tool call received",
                        tool=name,
                        call_id=call_id,
                        arguments=arguments,
                    )
                    result = self._run_tool(name, arguments)
                    _disaster_status(
                        "Agent tool call completed",
                        tool=name,
                        call_id=call_id,
                        ok=bool(result.get("ok", False)),
                        result=result,
                    )
                except Exception as exc:
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    _disaster_status(
                        "Agent tool call failed",
                        tool=name,
                        call_id=call_id,
                        error=result["error"],
                    )
                pending_actions.extend(_pending_actions_from_tool_result(result, tool_name=name))
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, default=str),
                    }
                )
            response = client.responses.create(
                model=self.model,
                input=tool_outputs,
                tools=DISASTER_TOOL_SCHEMAS,
                previous_response_id=_object_get(response, "id"),
                parallel_tool_calls=False,
            )
        _disaster_status(
            "Model request completed",
            model=self.model,
            pending_action_count=len(pending_actions),
        )
        return _response_text(response), pending_actions

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "discover_ads_agents":
            limit = max(1, min(int(arguments.get("limit") or 10), 50))
            agents = discover_ads_agents(
                skill=str(arguments.get("skill") or ""),
                query=str(arguments.get("query") or ""),
                limit=limit,
                include_self=bool(arguments.get("include_self", False)),
            )
            return {"ok": True, "agents": agents, "count": len(agents)}

        if name == "send_a2a_request":
            timeout_seconds = max(
                1,
                min(
                    int(arguments.get("timeout_seconds") or DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS),
                    120,
                ),
            )
            skill = str(arguments.get("skill") or "")
            message = str(arguments.get("message") or "")
            data = dict(arguments.get("data") or {})
            if skill == "email/group_send":
                message, data = _prepare_group_notice_a2a_payload(message=message, data=data)
            result = _send_a2a_request_direct(
                agent_ref=str(arguments.get("agent_ref") or ""),
                skill=skill,
                message=message,
                data=data,
                timeout_seconds=timeout_seconds,
            )
            return {"ok": True, **result}

        return {"ok": False, "error": f"Unknown tool: {name}"}

    def _normalize_plan(self, data: dict[str, Any]) -> dict[str, Any]:
        plan = data.get("response") if isinstance(data.get("response"), dict) else data
        normalized = {
            "summary": str(plan.get("summary") or ""),
            "risk_level": str(plan.get("risk_level") or "unknown").lower(),
            "incident_types": _as_string_list(plan.get("incident_types")),
            "immediate_actions": _as_string_list(plan.get("immediate_actions")),
            "shelter_guidance": _as_string_list(plan.get("shelter_guidance")),
            "evacuation_guidance": _as_string_list(plan.get("evacuation_guidance")),
            "communications": _as_string_list(plan.get("communications")),
            "medical_and_accessibility": _as_string_list(plan.get("medical_and_accessibility")),
            "supplies": _as_string_list(plan.get("supplies")),
            "avoid": _as_string_list(plan.get("avoid")),
            "follow_up_checks": _as_string_list(plan.get("follow_up_checks")),
            "disclaimer": str(
                plan.get("disclaimer")
                or "This is AI-generated emergency planning guidance, not a substitute for local emergency services or official instructions."
            ),
            "response_text": str(data.get("response_text") or plan.get("response_text") or ""),
        }
        if normalized["risk_level"] not in {"low", "medium", "high", "critical", "unknown"}:
            normalized["risk_level"] = "unknown"
        if not normalized["summary"]:
            normalized["summary"] = "Emergency response guidance based on the provided situation."
        return normalized


class DisasterResponseAgentExecutor(_AgentExecutorBase):
    """A2A AgentExecutor wrapper around the disaster response engine."""

    def __init__(self, agent: DisasterResponseAgent | None = None):
        _require_a2a()
        self.agent = agent or DisasterResponseAgent()

    async def execute(self, context: A2ARequestContext, event_queue: A2AEventQueue) -> None:
        updater = _task_updater(event_queue, context.task_id, context.context_id)
        skill = _a2a_skill_from_context(context)
        payload = _a2a_payload_from_context(context)
        user_text = _a2a_user_text(context)
        request_id = uuid.uuid4().hex[:12]
        outcome = "completed"
        _disaster_status(
            "A2A request received",
            request_id=request_id,
            skill=skill,
            task_id=context.task_id,
            context_id=context.context_id,
            user_text=user_text,
            payload=payload,
        )
        try:
            _disaster_status("A2A skill dispatching", request_id=request_id, skill=skill)
            if skill not in {
                "disaster/response_plan",
                "disaster/triage",
                "disaster/checklist",
            }:
                outcome = "unsupported"
                _disaster_status("A2A unsupported skill", request_id=request_id, skill=skill)
                await _a2a_complete(
                    updater,
                    skill=skill,
                    artifact_id="disaster-unsupported-skill",
                    artifact_name="disaster_unsupported_skill",
                    text=f"Unsupported disaster response skill: {skill}",
                    output={
                        "skill": skill,
                        "status": "unsupported",
                        "context_id": context.context_id,
                        "task_id": context.task_id,
                    },
                )
                return

            description = _first_text(
                payload,
                ("situation", "description", "incident", "event", "prompt", "message"),
            ) or user_text
            if not description.strip():
                outcome = "input_required"
                _disaster_status(
                    "A2A request missing input",
                    request_id=request_id,
                    skill=skill,
                    missing="description",
                )
                await _a2a_requires_input(
                    updater,
                    skill=skill,
                    artifact_id="disaster-description-required",
                    artifact_name="disaster_description_required",
                    text="Please provide a situation description for the disaster response plan.",
                    output={
                        "skill": skill,
                        "status": "input_required",
                        "missing": ["description"],
                        "context_id": context.context_id,
                        "task_id": context.task_id,
                    },
                )
                return

            result = await _maybe_to_thread(
                self.agent.build_response,
                description,
                location=str(payload.get("location") or ""),
                audience=str(payload.get("audience") or ""),
                severity=str(payload.get("severity") or ""),
                resources=_list_of_strings(payload.get("resources")),
            )
            result.update(
                {
                    "skill": skill,
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                }
            )
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="disaster-response-plan",
                artifact_name="disaster_response_plan",
                text=result["response_text"],
                output=result,
            )
        except Exception as exc:
            outcome = "error"
            _disaster_status(
                "A2A request failed",
                request_id=request_id,
                skill=skill,
                task_id=context.task_id,
                context_id=context.context_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            await _a2a_failed(
                updater,
                skill=skill,
                artifact_id="disaster-error",
                artifact_name="disaster_error",
                text=f"{type(exc).__name__}: {exc}",
                output={
                    "skill": skill,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
        finally:
            _disaster_status(
                "A2A request finished",
                request_id=request_id,
                skill=skill,
                task_id=context.task_id,
                context_id=context.context_id,
                outcome=outcome,
            )

    async def cancel(self, context: A2ARequestContext, event_queue: A2AEventQueue) -> None:
        _disaster_status(
            "A2A request cancelled",
            task_id=context.task_id,
            context_id=context.context_id,
        )
        updater = _task_updater(event_queue, context.task_id, context.context_id)
        await updater.reject()


async def _maybe_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(func, *args, **kwargs)


def _run_async(coro: Any) -> Any:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("Cannot run a remote A2A request from an active event loop.")


def load_disaster_a2a_agent_card(
    card_path: Path | str = DEFAULT_CARD_PATH,
) -> A2AAgentCard:
    _require_a2a()
    assert _AgentCard is not None
    with Path(card_path).open("r", encoding="utf-8") as fh:
        return _AgentCard.model_validate(json.load(fh))


def create_disaster_a2a_app(
    agent_card: A2AAgentCard,
    agent: DisasterResponseAgent | None = None,
) -> Any:
    _require_a2a()
    assert _A2AStarletteApplication is not None
    assert _DefaultRequestHandler is not None
    assert _InMemoryTaskStore is not None
    request_handler = _DefaultRequestHandler(
        agent_executor=DisasterResponseAgentExecutor(agent),
        task_store=_InMemoryTaskStore(),
    )
    return _A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()


def _with_advertised_url(agent_card: A2AAgentCard, advertised_url: str | None) -> A2AAgentCard:
    if not advertised_url:
        return agent_card
    return agent_card.model_copy(update={"url": advertised_url.rstrip("/") + "/"})


def _agent_card_json(agent_card: A2AAgentCard) -> dict[str, Any]:
    return agent_card.model_dump(mode="json", by_alias=True, exclude_none=True)


def _agent_card_to_oasf_record(agent_card: A2AAgentCard) -> dict[str, Any]:
    card_data = _agent_card_json(agent_card)
    provider = getattr(agent_card, "provider", None)
    organization = getattr(provider, "organization", None) if provider else "unknown"
    name = getattr(agent_card, "name", "")
    version = getattr(agent_card, "version", "")
    agent_id = name.lower().replace(" ", "-")
    custom_tags = sorted(
        set(
            tag
            for skill in getattr(agent_card, "skills", []) or []
            for tag in [getattr(skill, "id", ""), *(getattr(skill, "tags", []) or [])]
            if tag
        )
    )
    return {
        "schema_version": "demo-v2",
        "record_type": "agntcy.oasf.demo.agent_record",
        "record_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"v2-agent-network:{name}:{version}")),
        "agent_id": agent_id,
        "name": name,
        "organization": organization,
        "description": getattr(agent_card, "description", ""),
        "interaction": {
            "type": "a2a_agent_card",
            "endpoint": getattr(agent_card, "url", ""),
            "preferred_transport": (getattr(agent_card, "preferred_transport", None) or "JSONRPC").lower(),
            "agent_card": card_data,
        },
        "discovery": {
            "official_oasf_skills": [],
            "custom_skill_tags": custom_tags,
            "domains": sorted({tag.split("/")[0] for tag in custom_tags if "/" in tag}),
            "input_modes": getattr(agent_card, "default_input_modes", None) or [],
            "output_modes": getattr(agent_card, "default_output_modes", None) or [],
        },
        "auth": {
            "security": getattr(agent_card, "security", None) or [],
            "security_schemes": getattr(agent_card, "security_schemes", None) or {},
            "required_scopes": [],
        },
        "policy": {
            "risk_level": "low",
            "action_type": "read",
        },
        "metadata": {
            "source": "standalone_disaster_response_agent",
            "demo_specific_custom_skills": custom_tags,
        },
    }


def _agent_card_to_ads_record(agent_card: A2AAgentCard) -> dict[str, Any]:
    base_record = _agent_card_to_oasf_record(agent_card)
    card_data = base_record["interaction"]["agent_card"]
    provider = getattr(agent_card, "provider", None)
    provider_url = getattr(provider, "url", "") if provider else ""
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    custom_tags = base_record["discovery"]["custom_skill_tags"]
    return {
        "schema_version": "1.0.0",
        "name": provider_url.rstrip("/") if provider_url.startswith(("http://", "https://")) else "https://example.invalid/agents/disaster-response-agent",
        "version": getattr(agent_card, "version", ""),
        "authors": [base_record.get("organization") or "Disaster Response Agent"],
        "description": getattr(agent_card, "description", ""),
        "created_at": created_at,
        "skills": OFFICIAL_ADS_ROUTING_SKILLS,
        "domains": [],
        "locators": [
            {
                "type": "source_code",
                "urls": [provider_url or getattr(agent_card, "url", "")],
                "annotations": {"a2a.url": getattr(agent_card, "url", "")},
            }
        ],
        "modules": [
            {
                "name": "integration/a2a",
                "data": {
                    "card_schema_version": getattr(agent_card, "protocol_version", None) or "0.3.0",
                    "card_data": card_data,
                },
            }
        ],
        "annotations": {
            "a2a.url": getattr(agent_card, "url", ""),
            "agentic_network.agent_id": base_record["agent_id"],
            "agentic_network.record_id": base_record["record_id"],
            "agentic_network.display_name": getattr(agent_card, "name", ""),
            "agentic_network.organization": base_record.get("organization", "unknown"),
            "agentic_network.custom_skill_tags": ",".join(custom_tags),
        },
    }


def publish_disaster_a2a_agent_card_to_ads(
    agent_card: A2AAgentCard,
    *,
    ads_url: str | None = None,
    ads_backend: str | None = DEFAULT_ADS_BACKEND,
) -> PublishedRecord:
    backend = (ads_backend or os.getenv("ADS_BACKEND") or DEFAULT_ADS_BACKEND).lower()
    if backend != "cli":
        raise RuntimeError("Standalone ADS publication supports ADS_BACKEND=cli only.")

    ads_record = _agent_card_to_ads_record(agent_card)
    internal_record = _agent_card_to_oasf_record(agent_card)
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as fh:
        json.dump(ads_record, fh, indent=2, sort_keys=True)
        temp_path = Path(fh.name)
    try:
        _disaster_status("Pushing Disaster Response Agent ADS record", record_file=str(temp_path))
        push = _run_dirctl(["push", str(temp_path), "--output", "raw"], ads_url=ads_url)
        cid = push.stdout.strip()
        if not cid:
            raise RuntimeError("dirctl push did not return a CID.")
        _disaster_status("Publishing Disaster Response Agent ADS routing entry", cid=cid)
        _run_dirctl(["routing", "publish", cid], ads_url=ads_url)
    finally:
        temp_path.unlink(missing_ok=True)
    return PublishedRecord(
        record_id=internal_record["record_id"],
        agent_id=internal_record["agent_id"],
        backend="real",
        backend_ref=cid,
    )


def _run_dirctl(args: list[str], *, ads_url: str | None) -> subprocess.CompletedProcess[str]:
    server_address = (
        ads_url
        or os.getenv("ADS_SERVER_ADDR")
        or os.getenv("ADS_URL")
        or os.getenv("ADS_SERVER_ADDRESS")
        or os.getenv("DIRECTORY_CLIENT_SERVER_ADDRESS")
        or DEFAULT_ADS_SERVER_ADDR
    )
    command = [os.getenv("DIRCTL_BIN", "dirctl"), "--server-addr", server_address, *args]
    try:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("dirctl is not installed or not on PATH.") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"dirctl command failed: {' '.join(command)}\n{detail}")
    return completed


def unpublish_disaster_a2a_agent_card_from_ads(
    published: PublishedRecord | str | None,
    *,
    ads_url: str | None = None,
) -> bool:
    if published is None:
        return False
    cid = published if isinstance(published, str) else published.backend_ref
    if not cid:
        _disaster_status("Skipping ADS unpublish", reason="missing published CID")
        return False

    _disaster_status("Unpublishing Disaster Response Agent from ADS", cid=cid)
    try:
        _run_dirctl(["routing", "unpublish", cid], ads_url=ads_url)
    except Exception as exc:
        _disaster_status(
            "Failed to unpublish Disaster Response Agent from ADS",
            cid=cid,
            error=str(exc),
        )
        return False

    _disaster_status("Disaster Response Agent unpublished from ADS", cid=cid)
    return True


def _try_dirctl(
    args: list[str],
    *,
    ads_url: str | None,
    errors: list[str],
) -> subprocess.CompletedProcess[str] | None:
    try:
        return _run_dirctl(args, ads_url=ads_url)
    except RuntimeError as exc:
        errors.append(str(exc))
        return None


def _cids_from_routing_output(raw: str) -> list[str]:
    if not raw.strip():
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("items", "records", "results", "record_refs", "recordRefs", "refs"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
    items = data if isinstance(data, list) else [data]
    cids: list[str] = []
    for item in items:
        cid = _cid_from_ads_item(item)
        if cid:
            cids.append(cid)
    return _dedupe_strings(cids)


def _cid_from_ads_item(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return None
    for key in ("cid", "id"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    record_ref = item.get("record_ref") or item.get("recordRef") or item.get("ref")
    if isinstance(record_ref, dict):
        value = record_ref.get("cid") or record_ref.get("id")
        return value if isinstance(value, str) else None
    record = item.get("record")
    if isinstance(record, dict):
        value = record.get("cid") or record.get("id")
        return value if isinstance(value, str) else None
    return None


def _record_from_ads_pull_output(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("dirctl pull returned non-object JSON", raw, 0)
    for key in ("record", "data"):
        value = data.get(key)
        if isinstance(value, dict):
            if "data" in value and isinstance(value["data"], dict):
                return value["data"]
            return value
    return data


def _card_data_from_ads_record(record: dict[str, Any]) -> dict[str, Any] | None:
    interaction = record.get("interaction")
    if isinstance(interaction, dict) and isinstance(interaction.get("agent_card"), dict):
        return interaction["agent_card"]
    for module in record.get("modules", []) or []:
        if not isinstance(module, dict):
            continue
        data = module.get("data")
        if isinstance(data, dict) and isinstance(data.get("card_data"), dict):
            return data["card_data"]
    return None


def _a2a_url_from_ads_record(
    record: dict[str, Any],
    card_data: dict[str, Any] | None,
) -> str:
    annotations = record.get("annotations") if isinstance(record.get("annotations"), dict) else {}
    url = annotations.get("a2a.url")
    if isinstance(url, str) and url:
        return url
    interaction = record.get("interaction")
    if isinstance(interaction, dict) and isinstance(interaction.get("endpoint"), str):
        return interaction["endpoint"]
    for locator in record.get("locators", []) or []:
        if not isinstance(locator, dict):
            continue
        locator_annotations = locator.get("annotations")
        if isinstance(locator_annotations, dict):
            url = locator_annotations.get("a2a.url")
            if isinstance(url, str) and url:
                return url
    if card_data and isinstance(card_data.get("url"), str):
        return card_data["url"]
    return ""


def _ads_record_to_agent_summary(
    record: dict[str, Any],
    *,
    cid: str | None = None,
) -> dict[str, Any]:
    card_data = _card_data_from_ads_record(record) or {}
    annotations = record.get("annotations") if isinstance(record.get("annotations"), dict) else {}
    endpoint = _a2a_url_from_ads_record(record, card_data)
    name = str(
        card_data.get("name")
        or annotations.get("agentic_network.display_name")
        or record.get("name")
        or "Unknown Agent"
    )
    provider = card_data.get("provider") if isinstance(card_data.get("provider"), dict) else {}
    agent_id = str(
        annotations.get("agentic_network.agent_id")
        or record.get("agent_id")
        or name.lower().replace(" ", "-")
    )
    record_id = str(
        annotations.get("agentic_network.record_id")
        or record.get("record_id")
        or cid
        or agent_id
    )
    skills = _skill_summaries_from_card_data(card_data)
    custom_tags = _custom_skill_tags_from_ads_record(record, skills)
    return {
        "agent_id": agent_id,
        "record_id": record_id,
        "cid": cid or "",
        "name": name,
        "organization": str(
            annotations.get("agentic_network.organization")
            or record.get("organization")
            or provider.get("organization")
            or "unknown"
        ),
        "description": str(card_data.get("description") or record.get("description") or ""),
        "endpoint": endpoint,
        "source_card_url": f"{endpoint.rstrip('/')}/.well-known/agent-card.json" if endpoint else "",
        "skills": skills,
        "custom_skill_tags": custom_tags,
        "official_routing_skills": [
            item for item in record.get("skills", []) or [] if isinstance(item, dict)
        ],
    }


def _skill_summaries_from_card_data(card_data: dict[str, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for skill in card_data.get("skills", []) or []:
        if not isinstance(skill, dict):
            continue
        summaries.append(
            {
                "id": str(skill.get("id") or ""),
                "name": str(skill.get("name") or ""),
                "description": str(skill.get("description") or ""),
                "tags": [str(tag) for tag in skill.get("tags", []) or []],
                "examples": [str(example) for example in skill.get("examples", []) or []],
            }
        )
    return summaries


def _custom_skill_tags_from_ads_record(
    record: dict[str, Any],
    skills: list[dict[str, Any]],
) -> list[str]:
    tags: list[str] = []
    annotations = record.get("annotations") if isinstance(record.get("annotations"), dict) else {}
    annotated_tags = annotations.get("agentic_network.custom_skill_tags")
    if isinstance(annotated_tags, str):
        tags.extend(tag.strip() for tag in annotated_tags.split(",") if tag.strip())
    discovery = record.get("discovery")
    if isinstance(discovery, dict):
        tags.extend(str(tag) for tag in discovery.get("custom_skill_tags", []) or [])
    for skill in skills:
        if skill.get("id"):
            tags.append(str(skill["id"]))
        tags.extend(str(tag) for tag in skill.get("tags", []) or [])
    return sorted(set(tags))


def _dedupe_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _ads_skill_hints(skill: str) -> list[str]:
    if not skill:
        return []
    if skill.startswith(("disaster/", "email/")):
        return [str(item["name"]) for item in OFFICIAL_ADS_ROUTING_SKILLS]
    return []


def _looks_like_ads_cid(value: str) -> bool:
    return value.startswith(("bafy", "bae", "bag", "zd", "Qm"))


def _pull_ads_record(cid: str, *, ads_url: str | None = None) -> dict[str, Any]:
    result = _run_dirctl(["pull", cid, "--output", "json"], ads_url=ads_url)
    try:
        return _record_from_ads_pull_output(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"dirctl pull {cid} did not return a valid ADS record.") from exc


def _ads_search_cids(
    *,
    skill: str,
    limit: int,
    ads_url: str | None = None,
) -> list[str]:
    errors: list[str] = []
    cids: list[str] = []
    completed_any = False
    search_terms = _dedupe_strings([skill, *_ads_skill_hints(skill)])

    def collect(args: list[str]) -> None:
        nonlocal completed_any
        completed = _try_dirctl(args, ads_url=ads_url, errors=errors)
        if completed is None:
            return
        completed_any = True
        try:
            cids.extend(_cids_from_routing_output(completed.stdout))
        except json.JSONDecodeError as exc:
            errors.append(f"Could not parse ADS output from {' '.join(args)}: {exc}")

    if search_terms:
        for search_skill in search_terms:
            collect(["routing", "search", "--skill", search_skill, "--limit", str(limit), "--output", "json"])
            if not cids:
                collect(["routing", "list", "--skill", search_skill, "--output", "json"])
            if not cids:
                collect(["search", "--skill", search_skill, "--limit", str(limit), "--output", "json"])
    else:
        collect(["routing", "search", "--limit", str(limit), "--output", "json"])
        if not cids:
            collect(["routing", "list", "--output", "json"])
        if not cids:
            collect(["search", "--limit", str(limit), "--output", "json"])

    if search_terms and not cids:
        collect(["routing", "search", "--limit", str(limit), "--output", "json"])
        if not cids:
            collect(["routing", "list", "--output", "json"])

    if not completed_any and errors:
        raise RuntimeError(f"ADS discovery failed: {errors[0]}")
    return _dedupe_strings(cids)[:limit]


def _agent_summary_matches(summary: dict[str, Any], *, skill: str, query: str) -> bool:
    haystack_parts = [
        summary.get("agent_id", ""),
        summary.get("record_id", ""),
        summary.get("name", ""),
        summary.get("organization", ""),
        summary.get("description", ""),
        summary.get("endpoint", ""),
        " ".join(summary.get("custom_skill_tags", []) or []),
    ]
    for skill_item in summary.get("skills", []) or []:
        if isinstance(skill_item, dict):
            haystack_parts.extend(
                [
                    skill_item.get("id", ""),
                    skill_item.get("name", ""),
                    skill_item.get("description", ""),
                    " ".join(skill_item.get("tags", []) or []),
                    " ".join(skill_item.get("examples", []) or []),
                ]
            )
    for official in summary.get("official_routing_skills", []) or []:
        if isinstance(official, dict):
            haystack_parts.extend([str(official.get("name", "")), str(official.get("id", ""))])
    haystack = " ".join(str(part).lower() for part in haystack_parts)
    if skill and skill.lower() not in haystack:
        return False
    if query and query.lower() not in haystack:
        return False
    return True


def _current_agent_urls() -> set[str]:
    urls = {DEFAULT_AGENT_URL.rstrip("/")}
    env_url = os.getenv("DISASTER_AGENT_A2A_URL")
    if env_url:
        urls.add(env_url.rstrip("/"))
    try:
        if DEFAULT_CARD_PATH.exists():
            data = json.loads(DEFAULT_CARD_PATH.read_text(encoding="utf-8"))
            url = data.get("url")
            if isinstance(url, str) and url:
                urls.add(url.rstrip("/"))
    except Exception:
        pass
    return urls


def _is_current_agent_summary(summary: dict[str, Any]) -> bool:
    endpoint = str(summary.get("endpoint") or "").rstrip("/")
    return bool(endpoint and endpoint in _current_agent_urls())


def discover_ads_agents(
    *,
    skill: str = "",
    query: str = "",
    limit: int = 10,
    include_self: bool = False,
    ads_url: str | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 50))
    skill = skill.strip()
    query = query.strip()
    cids = _ads_search_cids(skill=skill, limit=max(limit * 3, limit), ads_url=ads_url)
    summaries: list[dict[str, Any]] = []
    for cid in cids:
        try:
            summary = _ads_record_to_agent_summary(_pull_ads_record(cid, ads_url=ads_url), cid=cid)
        except Exception:
            continue
        if not include_self and _is_current_agent_summary(summary):
            continue
        if not _agent_summary_matches(summary, skill=skill, query=query):
            continue
        summaries.append(summary)

    deduped: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        key = str(summary.get("cid") or summary.get("record_id") or summary.get("endpoint"))
        if key:
            deduped[key] = summary
    return list(deduped.values())[:limit]


def _resolve_ads_agent(
    agent_ref: str,
    *,
    ads_url: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    agent_ref = agent_ref.strip()
    if not agent_ref:
        raise ValueError("agent_ref is required.")

    if _looks_like_ads_cid(agent_ref):
        record = _pull_ads_record(agent_ref, ads_url=ads_url)
        summary = _ads_record_to_agent_summary(record, cid=agent_ref)
        card_data = _card_data_from_ads_record(record)
        if card_data is None:
            raise RuntimeError(f"ADS record {agent_ref} does not contain an embedded A2A Agent Card.")
        return record, summary, card_data

    candidates = discover_ads_agents(limit=50, include_self=True, ads_url=ads_url)
    normalized_ref = agent_ref.rstrip("/").lower()
    for summary in candidates:
        aliases = {
            str(summary.get("cid") or "").lower(),
            str(summary.get("record_id") or "").lower(),
            str(summary.get("agent_id") or "").lower(),
            str(summary.get("name") or "").lower(),
            str(summary.get("endpoint") or "").rstrip("/").lower(),
            str(summary.get("source_card_url") or "").rstrip("/").lower(),
        }
        if normalized_ref in aliases:
            cid = str(summary.get("cid") or "")
            if not cid:
                raise RuntimeError(f"Resolved {agent_ref!r}, but the ADS result did not include a CID.")
            record = _pull_ads_record(cid, ads_url=ads_url)
            card_data = _card_data_from_ads_record(record)
            if card_data is None:
                raise RuntimeError(f"ADS record for {agent_ref!r} does not contain an embedded A2A Agent Card.")
            return record, _ads_record_to_agent_summary(record, cid=cid), card_data

    raise KeyError(f"Agent not found in ADS: {agent_ref}")


async def send_a2a_request_to_ads_agent(
    *,
    agent_ref: str,
    skill: str,
    message: str,
    data: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS,
    ads_url: str | None = None,
    confirmed: bool = True,
) -> dict[str, Any]:
    if not skill.strip():
        raise ValueError("skill is required.")
    if not message.strip():
        raise ValueError("message is required.")
    _, summary, card_data = _resolve_ads_agent(agent_ref, ads_url=ads_url)
    if _is_current_agent_summary(summary):
        raise RuntimeError("Refusing to send a remote A2A request to this same disaster response agent.")

    try:
        import httpx
        from a2a.client import ClientConfig as SdkClientConfig
        from a2a.client import ClientFactory as SdkClientFactory
        from a2a.types import (
            AgentCard,
            DataPart,
            Message,
            Part,
            Role,
            TextPart,
            TransportProtocol,
        )
    except ImportError as exc:
        raise RuntimeError("Remote A2A requests require the a2a-sdk and httpx packages.") from exc

    card = AgentCard.model_validate(card_data)
    context_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    parts = [Part(root=TextPart(text=message, metadata={"skill": skill}))]
    if data:
        parts.append(Part(root=DataPart(data=data, metadata={"skill": skill})))
    preauthorized_metadata = {"confirmed": True}
    _disaster_status(
        "Sending remote A2A request",
        agent=summary.get("agent_id"),
        skill=skill,
        endpoint=summary.get("endpoint"),
        data=data or {},
        request_text=message,
    )
    sdk_message = Message(
        role=Role.user,
        message_id=str(uuid.uuid4()),
        context_id=context_id,
        task_id=None,
        parts=parts,
        metadata={
            "target_agent_id": summary.get("agent_id"),
            "skill": skill,
            "requested_task_id": task_id,
            **preauthorized_metadata,
        },
    )
    async with httpx.AsyncClient(timeout=timeout_seconds) as http_client:
        client_config = SdkClientConfig(
            streaming=False,
            polling=False,
            httpx_client=http_client,
            supported_transports=[TransportProtocol.jsonrpc],
            use_client_preference=True,
            accepted_output_modes=list(getattr(card, "default_output_modes", None) or []),
        )
        sdk_client = SdkClientFactory(client_config).create(card)
        response = None
        async for event in sdk_client.send_message(sdk_message):
            response = event
        if response is None:
            raise RuntimeError("A2A request completed without a response event.")
    result = _summarize_a2a_response(response)
    result["agent"] = {
        key: summary.get(key)
        for key in ("agent_id", "record_id", "cid", "name", "organization", "endpoint")
    }
    result["skill"] = skill
    result["confirmed"] = confirmed
    for action in result.get("pending_actions", []) or []:
        if isinstance(action, dict):
            action.setdefault("agent", result["agent"])
            action.setdefault("skill", skill)
    _disaster_status(
        "Remote A2A request completed",
        agent=summary.get("agent_id"),
        skill=skill,
        state=result.get("state"),
        structured_output=result.get("structured_output"),
    )
    return result


def _summarize_a2a_response(response: Any) -> dict[str, Any]:
    if isinstance(response, tuple) and response:
        response = response[0]
    root = getattr(response, "root", response)
    error = getattr(root, "error", None)
    if error is not None:
        code = getattr(error, "code", "")
        message = getattr(error, "message", str(error))
        raise RuntimeError(f"A2A error {code}: {message}")
    result = getattr(root, "result", root)
    status = getattr(result, "status", None)
    state = _a2a_value(getattr(status, "state", None)) or (
        "completed" if hasattr(result, "parts") else "unknown"
    )
    messages: list[dict[str, Any]] = []
    if hasattr(result, "parts"):
        messages.append(_summarize_a2a_message(result))
    for message in getattr(result, "history", None) or []:
        messages.append(_summarize_a2a_message(message))
    status_message = getattr(status, "message", None)
    if status_message is not None:
        messages.append(_summarize_a2a_message(status_message))
    artifacts = [_summarize_a2a_artifact(artifact) for artifact in getattr(result, "artifacts", None) or []]
    structured_output = _structured_output_from_a2a_result(result)
    text_parts = [
        part.get("text", "")
        for message_item in messages
        for part in message_item.get("parts", [])
        if part.get("kind") == "text" and part.get("text")
    ]
    summary = {
        "state": state,
        "task_id": getattr(result, "id", None) or getattr(result, "task_id", None),
        "context_id": getattr(result, "context_id", None),
        "text": "\n".join(text_parts),
        "messages": messages,
        "artifacts": artifacts,
        "structured_output": structured_output,
    }
    pending_actions = _a2a_pending_actions_from_summary(summary)
    summary["requires_follow_up"] = bool(pending_actions)
    summary["pending_actions"] = pending_actions
    return summary


def _summarize_a2a_message(message: Any) -> dict[str, Any]:
    return {
        "message_id": getattr(message, "message_id", ""),
        "role": _a2a_value(getattr(message, "role", "")),
        "parts": [_summarize_a2a_part(part) for part in getattr(message, "parts", []) or []],
        "metadata": getattr(message, "metadata", None) or {},
    }


def _summarize_a2a_artifact(artifact: Any) -> dict[str, Any]:
    return {
        "artifact_id": getattr(artifact, "artifact_id", ""),
        "name": getattr(artifact, "name", "artifact"),
        "parts": [_summarize_a2a_part(part) for part in getattr(artifact, "parts", []) or []],
        "metadata": getattr(artifact, "metadata", None) or {},
    }


def _summarize_a2a_part(part: Any) -> dict[str, Any]:
    root = getattr(part, "root", part)
    metadata = getattr(root, "metadata", None) or getattr(part, "metadata", None) or {}
    if hasattr(root, "text"):
        return {
            "kind": "text",
            "content_type": "text/plain",
            "text": str(getattr(root, "text", "")),
            "metadata": metadata,
        }
    if hasattr(root, "data"):
        return {
            "kind": "data",
            "content_type": "application/json",
            "data": getattr(root, "data", None) or {},
            "metadata": metadata,
        }
    file_obj = getattr(root, "file", None)
    if file_obj is not None:
        return {
            "kind": "file",
            "content_type": getattr(file_obj, "mime_type", None)
            or getattr(file_obj, "mimeType", None)
            or "application/octet-stream",
            "uri": getattr(file_obj, "uri", ""),
            "name": getattr(file_obj, "name", ""),
            "metadata": metadata,
        }
    return {"kind": type(root).__name__, "content_type": "application/octet-stream", "metadata": metadata}


def _structured_output_from_a2a_result(result: Any) -> dict[str, Any] | None:
    for artifact in getattr(result, "artifacts", None) or []:
        for part in getattr(artifact, "parts", []) or []:
            root = getattr(part, "root", part)
            data = getattr(root, "data", None)
            if isinstance(data, dict):
                return data
    status = getattr(result, "status", None)
    message = getattr(status, "message", None)
    metadata = getattr(message, "metadata", None) if message is not None else None
    if isinstance(metadata, dict) and isinstance(metadata.get("structured_output"), dict):
        return metadata["structured_output"]
    return None


def _a2a_pending_actions_from_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return []


def _a2a_summary_payloads(summary: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    structured_output = summary.get("structured_output")
    if isinstance(structured_output, dict):
        payloads.append(structured_output)

    for collection_name in ("artifacts", "messages"):
        for item in summary.get(collection_name, []) or []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            if isinstance(metadata, dict) and isinstance(metadata.get("structured_output"), dict):
                payloads.append(metadata["structured_output"])
            for part in item.get("parts", []) or []:
                if isinstance(part, dict) and isinstance(part.get("data"), dict):
                    payloads.append(part["data"])
    return payloads


def _a2a_state_needs_follow_up(state: str) -> bool:
    normalized = state.strip().lower().replace("_", "-").replace(" ", "-")
    return normalized in {"input-required", "auth-required", "working", "submitted"}


def _pending_actions_from_tool_result(
    result: dict[str, Any],
    *,
    tool_name: str,
) -> list[dict[str, Any]]:
    return []


def _send_a2a_request_direct(
    *,
    agent_ref: str,
    skill: str,
    message: str,
    data: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS,
    ads_url: str | None = None,
) -> dict[str, Any]:
    return _run_async(
        send_a2a_request_to_ads_agent(
            agent_ref=agent_ref,
            skill=skill,
            message=message,
            data=data or {},
            timeout_seconds=timeout_seconds,
            ads_url=ads_url,
            confirmed=True,
        )
    )


def _should_notify_campus(
    *,
    description: str,
    location: str,
    audience: str,
    plan: dict[str, Any],
) -> bool:
    if _is_planning_only_request(description):
        return False

    haystack = " ".join(
        [
            description,
            location,
            audience,
            " ".join(_as_string_list(plan.get("incident_types"))),
            " ".join(_as_string_list(plan.get("communications"))),
        ]
    ).lower()
    campus_terms = {
        "campus",
        "classroom",
        "classrooms",
        "student",
        "students",
        "faculty",
        "staff",
        "university",
        "college",
        "school",
        "dorm",
        "residence hall",
        "library",
    }
    return any(term in haystack for term in campus_terms)


def _is_planning_only_request(description: str) -> bool:
    lowered = description.lower()
    no_threat_markers = {
        "no real threat",
        "not a real threat",
        "not real",
        "hypothetical",
        "example scenario",
        "practice scenario",
        "training scenario",
        "simulation",
        "tabletop",
        "drill",
        "preparedness",
        "template",
    }
    if any(marker in lowered for marker in no_threat_markers):
        return True

    planning_markers = {
        "draft a response plan",
        "draft response plan",
        "write a response plan",
        "make a response plan",
        "create a response plan",
        "prepare a response plan",
        "sample response plan",
        "response plan for",
    }
    active_markers = {
        "active",
        "actual",
        "currently",
        "happening",
        "in progress",
        "in effect",
        "issued",
        "now",
        "real emergency",
        "real incident",
        "people are",
        "there is",
        "there are",
    }
    return any(marker in lowered for marker in planning_markers) and not any(
        marker in lowered for marker in active_markers
    )


def _mentions_email_agent(text: str) -> bool:
    lowered = text.lower()
    return "email agent" in lowered or "group notice email" in lowered


def _find_email_group_notice_agent(ads_url: str | None = None) -> dict[str, Any] | None:
    searches = [
        {"skill": "email/group_send", "query": ""},
        {"skill": "email/send", "query": "notice"},
        {"skill": "", "query": "Email Agent"},
        {"skill": "", "query": "email"},
    ]
    for search in searches:
        try:
            agents = discover_ads_agents(
                skill=search["skill"],
                query=search["query"],
                limit=10,
                include_self=False,
                ads_url=ads_url,
            )
        except Exception:
            continue
        for agent in agents:
            if _agent_summary_matches(agent, skill="email/group_send", query=""):
                return agent
        if agents:
            return agents[0]
    return None


def _send_campus_group_notice(
    *,
    description: str,
    location: str,
    audience: str,
    severity: str,
    plan: dict[str, Any],
    ads_url: str | None = None,
) -> dict[str, Any] | None:
    agent = _find_email_group_notice_agent(ads_url=ads_url)
    if not agent:
        return {
            "ok": False,
            "requires_follow_up": True,
            "pending_actions": [
                {
                    "type": "manual_action_required",
                    "skill": "email/group_send",
                    "reason": "No Email Agent with email/group_send was discovered in ADS.",
                    "next_step": "Send the campus notice manually or publish/start the Email Agent.",
                }
            ],
        }

    subject = _campus_notice_subject(plan=plan, severity=severity)
    body = _campus_notice_body(
        description=description,
        location=location,
        audience=audience,
        plan=plan,
    )
    html = _campus_notice_html(
        description=description,
        location=location,
        audience=audience,
        plan=plan,
    )
    message = (
        "Send the group notice email.\n"
        f"Subject: {subject}\n"
        "Body:\n"
        f"{body}"
    )
    return _send_a2a_request_direct(
        agent_ref=str(agent.get("cid") or agent.get("record_id") or agent.get("agent_id") or ""),
        skill="email/group_send",
        message=message,
        data={
            "subject": subject,
            "body": body,
            "html": html,
            "high_importance": True,
        },
        timeout_seconds=DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS,
        ads_url=ads_url,
    )


def _campus_notice_subject(*, plan: dict[str, Any], severity: str) -> str:
    incident = ", ".join(_as_string_list(plan.get("incident_types"))[:2])
    risk = str(plan.get("risk_level") or severity or "emergency").strip().title()
    if incident:
        return f"{risk} Campus Alert: {incident.title()}"
    return f"{risk} Campus Alert: Take Immediate Safety Action"


def _campus_notice_body(
    *,
    description: str,
    location: str,
    audience: str,
    plan: dict[str, Any],
) -> str:
    lines = [
        _normalize_email_plain_text(str(plan.get("summary") or description).strip()),
        "",
        "Immediate actions:",
        *_bullet_lines(plan.get("immediate_actions"), max_items=5),
        "",
        "Shelter guidance:",
        *_bullet_lines(plan.get("shelter_guidance"), max_items=4),
        "",
        "Avoid:",
        *_bullet_lines(plan.get("avoid"), max_items=4),
        "",
        "Follow official campus safety and emergency responder instructions. Call 911 for life-threatening emergencies.",
    ]
    if location:
        lines.insert(0, f"Location: {location}")
    if audience:
        lines.insert(0, f"Audience: {audience}")
    return _normalize_email_plain_text("\n".join(line for line in lines if line is not None))


def _campus_notice_html(
    *,
    description: str,
    location: str,
    audience: str,
    plan: dict[str, Any],
) -> str:
    summary = _normalize_email_plain_text(str(plan.get("summary") or description).strip())
    sections = [
        ("Immediate actions", _as_string_list(plan.get("immediate_actions"))[:5]),
        ("Shelter guidance", _as_string_list(plan.get("shelter_guidance"))[:4]),
        ("Avoid", _as_string_list(plan.get("avoid"))[:4]),
    ]
    parts = [
        '<div style="font-family: Arial, sans-serif; font-size: 16px; line-height: 1.45; color: #202124;">'
    ]
    if audience:
        parts.append(f'<p style="margin: 0 0 12px 0;"><strong>Audience:</strong> {html_lib.escape(audience)}</p>')
    if location:
        parts.append(f'<p style="margin: 0 0 12px 0;"><strong>Location:</strong> {html_lib.escape(location)}</p>')
    if summary:
        parts.append(f'<p style="margin: 0 0 16px 0;">{html_lib.escape(summary)}</p>')
    for title, items in sections:
        if not items:
            continue
        parts.append(f'<p style="margin: 18px 0 6px 0;"><strong>{html_lib.escape(title)}:</strong></p>')
        parts.append('<ul style="margin: 0 0 16px 22px; padding: 0;">')
        for item in items:
            clean_item = _normalize_email_plain_text(str(item))
            parts.append(f'<li style="margin: 0 0 7px 0;">{html_lib.escape(clean_item)}</li>')
        parts.append("</ul>")
    parts.append(
        '<p style="margin: 18px 0 0 0;">'
        "Follow official campus safety and emergency responder instructions. "
        "Call 911 for life-threatening emergencies."
        "</p>"
    )
    parts.append("</div>")
    return "\n".join(parts)


def _bullet_lines(value: Any, *, max_items: int) -> list[str]:
    items = _as_string_list(value)[:max_items]
    return [f"- {_normalize_email_plain_text(item)}" for item in items] or [
        "- Follow official emergency instructions."
    ]


def _prepare_group_notice_a2a_payload(
    *,
    message: str,
    data: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    subject = str(data.get("subject") or _extract_labeled_text(message, "Subject") or "").strip()
    body = str(data.get("body") or _extract_labeled_body(message) or message).strip()
    subject = subject or "Campus Alert: Take Safety Action"
    body = _normalize_email_plain_text(body)
    html = str(data.get("html") or "").strip() or _plain_text_notice_to_html(body)
    prepared = {
        **data,
        "subject": subject,
        "body": body,
        "html": html,
        "high_importance": bool(data.get("high_importance", True)),
    }
    prepared_message = (
        "Send the group notice email.\n"
        f"Subject: {subject}\n"
        "Body:\n"
        f"{body}"
    )
    return prepared_message, prepared


def _extract_labeled_text(text: str, label: str) -> str:
    match = re.search(rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _extract_labeled_body(text: str) -> str:
    match = re.search(r"(?ims)^\s*Body\s*:\s*(.+)\s*$", text)
    return match.group(1).strip() if match else ""


def _normalize_email_plain_text(text: str) -> str:
    raw_lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(stripped)
    if current:
        blocks.append(current)

    normalized_blocks = [_normalize_email_text_block(block) for block in blocks]
    return "\n\n".join(block for block in normalized_blocks if block).strip()


def _normalize_email_text_block(lines: list[str]) -> str:
    output: list[str] = []
    paragraph: list[str] = []
    current_bullet = ""

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            output.append(" ".join(paragraph))
            paragraph = []

    def flush_bullet() -> None:
        nonlocal current_bullet
        if current_bullet:
            output.append(f"- {current_bullet.strip()}")
            current_bullet = ""

    for line in lines:
        bullet_match = re.match(r"^(?:[-*•]|\d+[.)])\s+(.+)$", line)
        if bullet_match:
            flush_paragraph()
            flush_bullet()
            current_bullet = bullet_match.group(1).strip()
            continue
        if current_bullet:
            current_bullet = f"{current_bullet} {line.strip()}"
            continue
        if len(lines) == 1 and line.endswith(":"):
            output.append(line)
            continue
        paragraph.append(line)

    flush_paragraph()
    flush_bullet()
    return "\n".join(output)


def _plain_text_notice_to_html(text: str) -> str:
    normalized = _normalize_email_plain_text(text)
    parts = [
        '<div style="font-family: Arial, sans-serif; font-size: 16px; line-height: 1.45; color: #202124;">'
    ]
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            parts.append("</ul>")
            in_list = False

    for block in normalized.split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if len(lines) == 1 and lines[0].endswith(":"):
            close_list()
            parts.append(f'<p style="margin: 18px 0 6px 0;"><strong>{html_lib.escape(lines[0])}</strong></p>')
            continue
        for line in lines:
            if line.startswith("- "):
                if not in_list:
                    parts.append('<ul style="margin: 0 0 16px 22px; padding: 0;">')
                    in_list = True
                parts.append(f'<li style="margin: 0 0 7px 0;">{html_lib.escape(line[2:].strip())}</li>')
            else:
                close_list()
                parts.append(f'<p style="margin: 0 0 16px 0;">{html_lib.escape(line)}</p>')
    close_list()
    parts.append("</div>")
    return "\n".join(parts)


def _campus_notice_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    structured_output = result.get("structured_output")
    return {
        "ok": _campus_notice_was_sent(result),
        "state": result.get("state"),
        "skill": result.get("skill"),
        "agent": result.get("agent"),
        "requires_follow_up": False,
        "pending_actions": [],
        "text": result.get("text", ""),
        "message_id": _campus_notice_message_id(result),
        "structured_status": (
            structured_output.get("status")
            if isinstance(structured_output, dict)
            else None
        ),
        "structured_output": structured_output,
    }


def _append_campus_notice_result_to_text(text: str, result: dict[str, Any]) -> str:
    agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
    agent_name = str(agent.get("name") or agent.get("agent_id") or "Email Agent")
    message_id = _campus_notice_message_id(result)
    if _campus_notice_was_sent(result):
        suffix = f" Message ID: {message_id}." if message_id else ""
        line = f"Campus notice: {agent_name} sent the group notice email.{suffix}"
    else:
        detail = _campus_notice_error_detail(result)
        if detail:
            line = f"Campus notice: {agent_name} did not confirm that the email was sent. Detail: {detail}"
        else:
            line = f"Campus notice: attempted to contact {agent_name}; remote state was {result.get('state') or 'unknown'}."
    if line in text:
        return text
    return f"{text.rstrip()}\n\n{line}" if text.strip() else line


def _campus_notice_was_sent(result: dict[str, Any]) -> bool:
    structured_output = result.get("structured_output")
    if not isinstance(structured_output, dict):
        return False
    if str(structured_output.get("status") or "").lower() == "error":
        return False
    return bool(
        structured_output.get("message_id")
        or structured_output.get("sent_email")
        or structured_output.get("recipient_count")
    )


def _campus_notice_message_id(result: dict[str, Any]) -> str:
    structured_output = result.get("structured_output")
    if not isinstance(structured_output, dict):
        return ""
    return str(structured_output.get("message_id") or "").strip()


def _campus_notice_error_detail(result: dict[str, Any]) -> str:
    structured_output = result.get("structured_output")
    if isinstance(structured_output, dict):
        error = str(structured_output.get("error") or structured_output.get("message") or "").strip()
        if error:
            return error
        status = str(structured_output.get("status") or "").strip()
        if status and status.lower() != "completed":
            return f"structured status {status}"
    text = str(result.get("text") or "").strip()
    if text:
        return _preview_text(text, 240)
    return ""


def _preview_text(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _pending_action_follow_up_lines(actions: list[dict[str, Any]]) -> list[str]:
    return [_format_pending_action_line(action) for action in actions if isinstance(action, dict)]


def _append_pending_actions_to_text(text: str, actions: list[dict[str, Any]]) -> str:
    lines = _pending_action_follow_up_lines(actions)
    if not lines:
        return text
    block = "Pending action:\n" + "\n".join(f"- {line}" for line in lines)
    if block in text:
        return text
    return f"{text.rstrip()}\n\n{block}" if text.strip() else block


def _format_pending_action_line(action: dict[str, Any]) -> str:
    agent = action.get("agent") if isinstance(action.get("agent"), dict) else {}
    agent_name = str(agent.get("name") or agent.get("agent_id") or "Remote agent")
    skill = str(action.get("skill") or "").strip()
    raw_action_type = str(action.get("type") or "follow_up_required")
    action_type = raw_action_type.replace("_", " ")
    reason = str(action.get("reason") or action.get("text") or "").strip()
    next_step = str(action.get("next_step") or "").strip()
    prefix = f"{agent_name} is waiting for {action_type}"
    if skill:
        prefix += f" for {skill}"
    pieces = [prefix]
    if reason:
        pieces.append(reason)
    if next_step:
        pieces.append(next_step)
    return ". ".join(piece.rstrip(".") for piece in pieces if piece) + "."


def _a2a_value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _require_a2a() -> None:
    if A2A_IMPORT_ERROR is not None:
        raise RuntimeError(
            "A2A server mode requires a2a-sdk, starlette, and uvicorn."
        ) from A2A_IMPORT_ERROR


def _task_updater(
    event_queue: A2AEventQueue,
    task_id: str,
    context_id: str,
) -> A2ATaskUpdater:
    _require_a2a()
    assert _TaskUpdater is not None
    return _TaskUpdater(event_queue, task_id, context_id)


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


def _is_data_part(value: Any) -> bool:
    return _DataPart is not None and isinstance(value, _DataPart)


def _a2a_skill_from_context(context: A2ARequestContext) -> str:
    for metadata in _a2a_metadata_sources(context):
        skill = metadata.get("skill")
        if skill:
            return str(skill)
    return "disaster/response_plan"


def _a2a_user_text(context: A2ARequestContext) -> str:
    try:
        return str(context.get_user_input())
    except Exception:
        return ""


def _a2a_payload_from_context(context: A2ARequestContext) -> dict[str, Any]:
    payload: dict[str, Any] = dict(context.metadata or {})
    if context.message:
        message_metadata = getattr(context.message, "metadata", None)
        if isinstance(message_metadata, dict):
            payload.update(message_metadata)
        for part in context.message.parts:
            root = part.root
            part_metadata = getattr(root, "metadata", None) or getattr(part, "metadata", None)
            if isinstance(part_metadata, dict):
                payload.update(part_metadata)
            if _is_data_part(root) and isinstance(root.data, dict):
                payload.update(root.data)
    return payload


def _a2a_metadata_sources(context: A2ARequestContext) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    if isinstance(context.metadata, dict):
        sources.append(context.metadata)
    if context.message:
        message_metadata = getattr(context.message, "metadata", None)
        if isinstance(message_metadata, dict):
            sources.append(message_metadata)
        for part in context.message.parts:
            root = part.root
            part_metadata = getattr(root, "metadata", None) or getattr(part, "metadata", None)
            if isinstance(part_metadata, dict):
                sources.append(part_metadata)
    return sources


async def _a2a_complete(
    updater: A2ATaskUpdater,
    *,
    skill: str,
    artifact_id: str,
    artifact_name: str,
    text: str,
    output: dict[str, Any],
) -> None:
    await updater.start_work()
    await updater.add_artifact(
        [_data_part(output)],
        artifact_id=artifact_id,
        name=artifact_name,
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.complete(
        message=updater.new_agent_message(
            [_text_part(text)],
            metadata={"structured_output": output},
        )
    )


async def _a2a_failed(
    updater: A2ATaskUpdater,
    *,
    skill: str,
    artifact_id: str,
    artifact_name: str,
    text: str,
    output: dict[str, Any],
) -> None:
    await updater.start_work()
    await updater.add_artifact(
        [_data_part(output)],
        artifact_id=artifact_id,
        name=artifact_name,
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.failed(
        message=updater.new_agent_message(
            [_text_part(text)],
            metadata={"structured_output": output},
        )
    )


async def _a2a_requires_input(
    updater: A2ATaskUpdater,
    *,
    skill: str,
    artifact_id: str,
    artifact_name: str,
    text: str,
    output: dict[str, Any],
) -> None:
    await updater.add_artifact(
        [_data_part(output)],
        artifact_id=artifact_id,
        name=artifact_name,
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.requires_input(
        message=updater.new_agent_message(
            [_text_part(text)],
            metadata={"structured_output": output},
        ),
        final=True,
    )


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _list_of_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Disaster response planning agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    respond_parser = subparsers.add_parser("respond", help="Build a disaster response plan.")
    respond_parser.add_argument("description", nargs="*")
    respond_parser.add_argument("--model", default=os.getenv("DISASTER_AGENT_MODEL", DEFAULT_MODEL))
    respond_parser.add_argument("--location", default="")
    respond_parser.add_argument("--audience", default="")
    respond_parser.add_argument("--severity", default="")
    respond_parser.add_argument("--resource", action="append", default=[])
    respond_parser.add_argument(
        "--ads-url",
        default=(
            os.getenv("ADS_SERVER_ADDR")
            or os.getenv("ADS_URL")
            or os.getenv("ADS_SERVER_ADDRESS")
            or os.getenv("DIRECTORY_CLIENT_SERVER_ADDRESS")
            or DEFAULT_ADS_SERVER_ADDR
        ),
    )
    respond_parser.add_argument("--json", action="store_true", help="Print structured JSON.")

    discover_parser = subparsers.add_parser("ads-discover", help="Discover agents from ADS.")
    discover_parser.add_argument("--skill", default="")
    discover_parser.add_argument("--query", default="")
    discover_parser.add_argument("--limit", type=int, default=10)
    discover_parser.add_argument("--include-self", action="store_true")
    discover_parser.add_argument(
        "--ads-url",
        default=(
            os.getenv("ADS_SERVER_ADDR")
            or os.getenv("ADS_URL")
            or os.getenv("ADS_SERVER_ADDRESS")
            or os.getenv("DIRECTORY_CLIENT_SERVER_ADDRESS")
            or DEFAULT_ADS_SERVER_ADDR
        ),
    )

    send_parser = subparsers.add_parser("a2a-send", help="Send an A2A request to an ADS-discovered agent.")
    send_parser.add_argument("agent_ref")
    send_parser.add_argument("--skill", required=True)
    send_parser.add_argument("--message", required=True)
    send_parser.add_argument("--data-json", default="{}")
    send_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS)
    send_parser.add_argument(
        "--ads-url",
        default=(
            os.getenv("ADS_SERVER_ADDR")
            or os.getenv("ADS_URL")
            or os.getenv("ADS_SERVER_ADDRESS")
            or os.getenv("DIRECTORY_CLIENT_SERVER_ADDRESS")
            or DEFAULT_ADS_SERVER_ADDR
        ),
    )

    a2a_parser = subparsers.add_parser("serve-a2a", help="Run as an A2A HTTP server.")
    a2a_parser.add_argument("--model", default=os.getenv("DISASTER_AGENT_MODEL", DEFAULT_MODEL))
    a2a_parser.add_argument("--host", default=os.getenv("DISASTER_AGENT_A2A_HOST", DEFAULT_HOST))
    a2a_parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("DISASTER_AGENT_A2A_PORT", str(DEFAULT_PORT))),
    )
    a2a_parser.add_argument("--card", type=Path, default=DEFAULT_CARD_PATH)
    a2a_parser.add_argument("--advertise-url", default=os.getenv("DISASTER_AGENT_A2A_URL"))
    a2a_parser.add_argument(
        "--ads-url",
        default=(
            os.getenv("ADS_SERVER_ADDR")
            or os.getenv("ADS_URL")
            or os.getenv("ADS_SERVER_ADDRESS")
            or os.getenv("DIRECTORY_CLIENT_SERVER_ADDRESS")
            or DEFAULT_ADS_SERVER_ADDR
        ),
    )
    a2a_parser.add_argument("--ads-backend", choices=["cli"], default=DEFAULT_ADS_BACKEND)
    a2a_parser.add_argument("--no-publish-ads", action="store_true")

    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "respond":
        agent = DisasterResponseAgent(model=args.model)
        description = " ".join(args.description).strip()
        result = agent.build_response(
            description,
            location=args.location,
            audience=args.audience,
            severity=args.severity,
            resources=args.resource,
            ads_url=args.ads_url,
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result["response_text"])
        return

    if args.command == "ads-discover":
        agents = discover_ads_agents(
            skill=args.skill,
            query=args.query,
            limit=args.limit,
            include_self=args.include_self,
            ads_url=args.ads_url,
        )
        print(json.dumps({"agents": agents, "count": len(agents)}, indent=2))
        return

    if args.command == "a2a-send":
        try:
            data = json.loads(args.data_json)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(f"Invalid --data-json: {exc}") from exc
        if not isinstance(data, dict):
            raise argparse.ArgumentTypeError("--data-json must decode to a JSON object.")
        result = _send_a2a_request_direct(
            agent_ref=args.agent_ref,
            skill=args.skill,
            message=args.message,
            data=data,
            timeout_seconds=args.timeout_seconds,
            ads_url=args.ads_url,
        )
        print(json.dumps(result, indent=2, default=str))
        return

    if args.command == "serve-a2a":
        _require_a2a()
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError("The uvicorn package is required for serve-a2a.") from exc

        agent = DisasterResponseAgent(model=args.model)
        card = _with_advertised_url(
            load_disaster_a2a_agent_card(args.card),
            args.advertise_url,
        )
        published: PublishedRecord | None = None
        if not args.no_publish_ads:
            published = publish_disaster_a2a_agent_card_to_ads(
                card,
                ads_url=args.ads_url,
                ads_backend=args.ads_backend,
            )
            cleanup_done = False

            def cleanup_ads_publication() -> None:
                nonlocal cleanup_done
                if cleanup_done:
                    return
                cleanup_done = True
                unpublish_disaster_a2a_agent_card_from_ads(
                    published,
                    ads_url=args.ads_url,
                )

            atexit.register(cleanup_ads_publication)
            _disaster_status(
                "Disaster Response Agent card published to ADS",
                backend=published.backend,
                record=published.backend_ref,
            )
        else:
            _disaster_status("Skipping ADS publication", reason="--no-publish-ads")
        _disaster_status(
            "Starting Disaster Response Agent A2A server",
            host=args.host,
            port=args.port,
            agent_url=card.url,
        )
        try:
            uvicorn.run(create_disaster_a2a_app(card, agent), host=args.host, port=args.port)
        finally:
            if published is not None:
                cleanup_ads_publication()
        return


if __name__ == "__main__":
    main()
