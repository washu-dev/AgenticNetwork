#!/usr/bin/env python3
"""Standalone geospatial image analysis agent with optional A2A serving.

This wraps attack_bundle/src/main_simple.py's existing SimpleGeo pipeline
without changing that pipeline's default Gemini geolocation model.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

_AGENT_DIR = Path(__file__).resolve().parent
_ATTACK_BUNDLE = _AGENT_DIR / "attack_bundle"
_ATTACK_SRC = _ATTACK_BUNDLE / "src"
sys.path.insert(0, str(_ATTACK_SRC))

from config import GeoConfig  # type: ignore  # noqa: E402
from models import GeoResult  # type: ignore  # noqa: E402
from pipeline_simple import SimpleGeo, _GeminiBackend, _OpenAIBackend  # type: ignore  # noqa: E402
from tools import load_image_bytes  # type: ignore  # noqa: E402

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
DEFAULT_PORT = 9105
DEFAULT_AGENT_URL = "http://10.229.117.223:9105/"
DEFAULT_CARD_PATH = _AGENT_DIR / "geospatial_agent.card.json"
DEFAULT_ADS_SERVER_ADDR = "10.229.117.37:8888"
DEFAULT_ADS_BACKEND = "cli"
DEFAULT_GEO_MODEL = "gemini-3-flash-preview"
DEFAULT_SCENE_MODEL = "gemini-3-flash-preview"
DEFAULT_COORDINATION_MODEL = "gpt-5.2"
DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS = 180
DEFAULT_TOOL_TOKEN_USAGE_LOG = _AGENT_DIR / "tool_token_usage.jsonl"
DEFAULT_LOG_VALUE_CHARS = 900
GEOSPATIAL_STATUS_LOG_FALSE_VALUES = {"0", "false", "no", "off"}
REMOTE_AGENT_TOOL_NAMES = {"discover_ads_agents", "send_a2a_request"}
LOG_REDACT_KEY_PARTS = {
    "authorization",
    "client_secret",
    "credential",
    "password",
    "refresh_token",
    "token",
    "api_key",
}
LOG_TOKEN_COUNT_KEYS = {
    "cached_input_tokens",
    "candidates_token_count",
    "completion_token_count",
    "completion_tokens",
    "input_tokens",
    "output_tokens",
    "prompt_token_count",
    "prompt_tokens",
    "total_token_count",
    "total_tokens",
}

OFFICIAL_ADS_ROUTING_SKILLS = [
    {
        "name": "natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search",
        "id": 10306,
    },
    {
        "name": "natural_language_processing/natural_language_generation/summarization",
        "id": 10202,
    },
    {
        "name": "retrieval_augmented_generation/retrieval_of_information/document_retrieval",
        "id": 60103,
    },
]


GEOSPATIAL_COORDINATION_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "discover_ads_agents",
        "description": (
            "Search ADS for registered agents that can help with the user's request. "
            "Use this before send_a2a_request."
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
                    "description": "Optional free-text filter over agent names, descriptions, and skill text.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum agents to return.",
                },
                "include_self": {
                    "type": "boolean",
                    "description": "Whether to include this GeoSpatial Agent in results.",
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
            "Send a request to another ADS-discovered A2A agent. Use an agent_ref from "
            "discover_ads_agents. The skill is advisory only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_ref": {
                    "type": "string",
                    "description": "Agent reference from ADS discovery: cid, record_id, agent_id, name, or endpoint.",
                },
                "skill": {
                    "type": "string",
                    "description": "Optional advisory target skill tag from the discovered agent card.",
                },
                "message": {
                    "type": "string",
                    "description": "Natural-language instruction to send to the remote agent.",
                },
                "data": {
                    "type": "object",
                    "description": "Optional structured JSON payload for the remote agent.",
                    "properties": {},
                    "additionalProperties": False,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 600,
                    "description": "A2A request timeout in seconds.",
                },
            },
            "required": ["agent_ref", "message", "data", "timeout_seconds"],
            "additionalProperties": False,
        },
        "strict": False,
    },
]


def _geo_status(message: str, **details: Any) -> None:
    if os.getenv("GEOSPATIAL_AGENT_STATUS_LOGS", "1").strip().lower() in GEOSPATIAL_STATUS_LOG_FALSE_VALUES:
        return
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    rendered = " ".join(
        f"{key}={_log_render(value)}"
        for key, value in details.items()
        if value is not None and value != ""
    )
    suffix = f" {rendered}" if rendered else ""
    print(f"[geospatial-agent] {timestamp} {message}{suffix}", file=sys.stderr, flush=True)


def _log_render(value: Any) -> str:
    sanitized = _log_sanitize(value)
    if isinstance(sanitized, str):
        rendered = sanitized.replace("\n", "\\n")
    else:
        rendered = json.dumps(sanitized, default=str, sort_keys=True)
    max_chars = int(os.getenv("GEOSPATIAL_AGENT_LOG_MAX_CHARS", str(DEFAULT_LOG_VALUE_CHARS)))
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
            if key_text not in LOG_TOKEN_COUNT_KEYS and any(part in key_text.lower() for part in LOG_REDACT_KEY_PARTS):
                sanitized[key_text] = "[redacted]"
            else:
                sanitized[key_text] = _log_sanitize(item, depth=depth + 1)
        return sanitized
    if isinstance(value, list):
        return [_log_sanitize(item, depth=depth + 1) for item in value[:20]]
    return value


def _coerce_token_count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _raw_usage_dict(usage: Any) -> dict[str, Any]:
    if not usage:
        return {}
    if isinstance(usage, dict):
        return usage
    for method_name in ("model_dump", "dict"):
        method = getattr(usage, method_name, None)
        if callable(method):
            try:
                dumped = method()
            except TypeError:
                continue
            if isinstance(dumped, dict):
                return dumped
    return {}


def _first_token_count(raw: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = _coerce_token_count(raw.get(name))
        if value is not None:
            return value
    return None


def _response_token_usage(response: Any) -> dict[str, Any]:
    usage = _object_get(response, "usage") or _object_get(response, "usage_metadata")
    raw = _raw_usage_dict(usage)
    if not raw:
        return {"usage_available": False}

    input_tokens = _first_token_count(raw, "input_tokens", "prompt_tokens", "prompt_token_count")
    output_tokens = _first_token_count(
        raw,
        "output_tokens",
        "completion_tokens",
        "completion_token_count",
        "candidates_token_count",
    )
    total_tokens = _first_token_count(raw, "total_tokens", "total_token_count")
    input_details = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
    cached_input_tokens = (
        _coerce_token_count(input_details.get("cached_tokens"))
        if isinstance(input_details, dict)
        else None
    )
    return {
        "usage_available": True,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
    }


def _model_provider(model: str) -> str:
    normalized = model.lower()
    if normalized.startswith(("gemini-", "models/gemini")):
        return "gemini"
    return "openai"


def _tool_token_usage_log_path() -> Path | None:
    configured = os.getenv("GEOSPATIAL_AGENT_TOOL_TOKEN_USAGE_LOG")
    if configured is not None:
        stripped = configured.strip()
        if stripped.lower() in GEOSPATIAL_STATUS_LOG_FALSE_VALUES:
            return None
        if stripped:
            return Path(stripped).expanduser()
    return DEFAULT_TOOL_TOKEN_USAGE_LOG


def _write_token_usage_event(event: dict[str, Any]) -> None:
    log_path = _tool_token_usage_log_path()
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_log_sanitize(event), default=str, sort_keys=True) + "\n")
    except Exception as exc:
        _geo_status("Token usage log write failed", error=f"{type(exc).__name__}: {exc}")


def _record_tool_call_token_usage(
    *,
    model: str,
    response: Any,
    tool: str,
    call_id: str,
    step: int,
    tool_call_count: int,
) -> dict[str, Any]:
    usage = _response_token_usage(response)
    event = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "agent": "geospatial-agent",
        "event": "tool_call_token_usage",
        "usage_scope": "model_response_that_requested_tool",
        "model": model,
        "provider": _model_provider(model),
        "response_id": _object_get(response, "id", ""),
        "step": step,
        "tool_call_count": tool_call_count,
        "tool": tool,
        "call_id": call_id,
        **usage,
    }
    _geo_status(
        "Peer coordination tool call token usage",
        model=model,
        tool=tool,
        call_id=call_id,
        step=step,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
        usage_available=usage.get("usage_available"),
    )
    _write_token_usage_event(event)
    return usage


def _record_image_analysis_token_usage_event(progress_event: dict[str, Any]) -> None:
    if progress_event.get("event") != "image_analysis_token_usage":
        return
    usage = {
        "usage_available": progress_event.get("usage_available"),
        "input_tokens": progress_event.get("input_tokens"),
        "output_tokens": progress_event.get("output_tokens"),
        "total_tokens": progress_event.get("total_tokens"),
        "cached_input_tokens": progress_event.get("cached_input_tokens"),
    }
    event = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "agent": "geospatial-agent",
        "event": "image_analysis_token_usage",
        "usage_scope": progress_event.get("usage_scope") or "image_analysis_model_call",
        "stage": progress_event.get("stage"),
        "model": progress_event.get("model"),
        "provider": progress_event.get("provider"),
        "streamed": progress_event.get("streamed"),
        **usage,
    }
    _geo_status(
        "Image analysis token usage",
        model=event.get("model"),
        provider=event.get("provider"),
        stage=event.get("stage"),
        streamed=event.get("streamed"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
        usage_available=usage.get("usage_available"),
    )
    _write_token_usage_event(event)


def _wrap_geospatial_progress_callback(
    callback: Callable[[dict[str, Any]], None] | None,
) -> Callable[[dict[str, Any]], None]:
    def wrapped(event: dict[str, Any]) -> None:
        _record_image_analysis_token_usage_event(event)
        if callback is not None:
            callback(event)

    return wrapped


def _parse_json(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                pass
    return {}


def _dedupe_strings(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


SCENE_PROMPT = """\
Analyze this image for public-safety and geospatial context.

Return ONLY valid JSON with this shape:
{
  "summary": "One or two plain-language sentences describing what is visibly happening.",
  "incident_types": ["flood" | "tornado_or_severe_storm" | "wildfire" | "fire_or_explosion" | "active_shooter_or_armed_threat" | "chemical_hazard" | "earthquake_damage" | "traffic_crash" | "civil_unrest_or_crowd" | "medical_emergency" | "infrastructure_damage" | "none_visible" | "unknown"],
  "visible_hazards": ["..."],
  "visible_context": ["buildings, roads, terrain, weather, emergency vehicles, people, signs, damage, water, smoke, debris, weapons if plainly visible"],
  "what_happened": "Best cautious interpretation of the event, with uncertainty.",
  "confidence": 0.0,
  "uncertainty": "What cannot be determined from the image alone.",
  "recommended_next_agents": ["disaster-response-agent", "email-agent", "..."]
}

Use only visible evidence. Do not identify private people. Do not provide tactical violence advice.
If the image does not clearly show a disaster or emergency, say so and use incident_types ["none_visible"] or ["unknown"].
"""


class GeoSpatialAgent:
    """Image geolocation plus public-safety scene interpretation."""

    def __init__(
        self,
        *,
        geo_model: str | None = None,
        scene_model: str | None = None,
        max_image_dim: int = 0,
        enable_remote_agent_tools: bool | None = None,
    ) -> None:
        self.geo_model = geo_model or os.getenv("GEOSPATIAL_GEO_MODEL", DEFAULT_GEO_MODEL)
        self.scene_model = scene_model or os.getenv("GEOSPATIAL_SCENE_MODEL", DEFAULT_SCENE_MODEL)
        self.max_image_dim = max_image_dim
        self.enable_remote_agent_tools = bool(enable_remote_agent_tools)

    def analyze_image(
        self,
        image_path: str,
        *,
        request_text: str = "",
        image_condition: str = "",
        skill: str = "geospatial/analyze_image",
        ads_url: str | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        image = _resolve_existing_image_path(image_path)
        if not image:
            raise ValueError(f"Image not found: {image_path}")
        progress_callback = _wrap_geospatial_progress_callback(progress_callback)

        run_location = skill in {
            "geospatial/analyze_image",
            "geospatial/location_inference",
            "image/geospatial_analysis",
            "",
        }
        run_scene = skill in {
            "geospatial/analyze_image",
            "geospatial/incident_assessment",
            "image/geospatial_analysis",
            "",
        }

        _geo_status(
            "Image analysis started",
            image_path=str(image),
            skill=skill,
            geo_model=self.geo_model if run_location else None,
            scene_model=self.scene_model if run_scene else None,
        )
        _emit_geospatial_progress(
            progress_callback,
            event="started",
            text="GeoSpatial image analysis started.",
            image_filename=image.name,
            skill=skill,
            geo_model=self.geo_model if run_location else None,
            scene_model=self.scene_model if run_scene else None,
        )
        location_result: dict[str, Any] | None = None
        scene_result: dict[str, Any] | None = None
        errors: list[str] = []

        if run_location:
            try:
                _emit_geospatial_progress(
                    progress_callback,
                    event="location_started",
                    text=f"Location inference started with {self.geo_model}.",
                    model=self.geo_model,
                )
                location_result = self._infer_location(
                    str(image),
                    image_condition=image_condition,
                    progress_callback=progress_callback,
                )
                _emit_geospatial_progress(
                    progress_callback,
                    event="location_completed",
                    text=_location_progress_text(location_result),
                    model=self.geo_model,
                    location=_compact_location_result(location_result),
                )
            except Exception as exc:
                errors.append(f"location_inference: {type(exc).__name__}: {exc}")
                _geo_status("Location inference failed", error=errors[-1])
                _emit_geospatial_progress(
                    progress_callback,
                    event="location_failed",
                    text=f"Location inference failed: {type(exc).__name__}: {exc}",
                    model=self.geo_model,
                )

        if run_scene:
            try:
                _emit_geospatial_progress(
                    progress_callback,
                    event="scene_started",
                    text=f"Scene assessment started with {self.scene_model}.",
                    model=self.scene_model,
                )
                scene_result = self._describe_scene(str(image), progress_callback=progress_callback)
                _emit_geospatial_progress(
                    progress_callback,
                    event="scene_completed",
                    text=_scene_progress_text(scene_result),
                    model=self.scene_model,
                    scene=_compact_scene_result(scene_result),
                )
            except Exception as exc:
                errors.append(f"scene_assessment: {type(exc).__name__}: {exc}")
                _geo_status("Scene assessment failed", error=errors[-1])
                _emit_geospatial_progress(
                    progress_callback,
                    event="scene_failed",
                    text=f"Scene assessment failed: {type(exc).__name__}: {exc}",
                    model=self.scene_model,
                )

        if request_text.strip() and self.enable_remote_agent_tools:
            _emit_geospatial_progress(
                progress_callback,
                event="coordination_started",
                text="Coordination model is deciding whether peer agents are needed.",
            )
        peer_results = self._maybe_call_peer_agents(
            request_text=request_text,
            location_result=location_result,
            scene_result=scene_result,
            ads_url=ads_url,
        )
        if request_text.strip() and self.enable_remote_agent_tools:
            _emit_geospatial_progress(
                progress_callback,
                event="coordination_completed",
                text=f"Peer coordination completed with {len(peer_results)} result(s).",
                peer_result_count=len(peer_results),
            )
        output = {
            "skill": skill or "geospatial/analyze_image",
            "status": "completed" if (location_result or scene_result) else "error",
            "image_path": str(image),
            "location_inference": location_result,
            "scene_assessment": scene_result,
            "peer_results": peer_results,
            "errors": errors,
        }
        output["response_text"] = _format_analysis_text(output)
        _geo_status(
            "Image analysis completed",
            status=output["status"],
            has_location=bool(location_result),
            has_scene=bool(scene_result),
            peer_result_count=len(peer_results),
        )
        _emit_geospatial_progress(
            progress_callback,
            event="completed",
            text=f"GeoSpatial image analysis {output['status']}.",
            status=output["status"],
            has_location=bool(location_result),
            has_scene=bool(scene_result),
            peer_result_count=len(peer_results),
        )
        return output

    def _infer_location(
        self,
        image_path: str,
        *,
        image_condition: str = "",
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        cfg = GeoConfig(
            model=self.geo_model,
            image_condition=image_condition,
            verbose=False,
            max_image_dim=self.max_image_dim,
            progress_callback=progress_callback,
        )
        result: GeoResult = SimpleGeo(cfg).geolocate(image_path)
        return {
            "latitude": result.latitude,
            "longitude": result.longitude,
            "description": result.description,
            "precision_level": result.precision_level,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "hypothesis_chain": result.hypothesis_chain,
            "alternatives": result.alternatives,
        }

    def _describe_scene(
        self,
        image_path: str,
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        provider = "openai" if self.scene_model.startswith(("gpt-", "o")) else "gemini"
        image_bytes = load_image_bytes(image_path, max_dim=self.max_image_dim)
        if provider == "openai":
            backend = _OpenAIBackend(self.scene_model)
        else:
            backend = _GeminiBackend(self.scene_model)
        try:
            backend.upload_image(image_bytes)
            raw = backend.call(
                SCENE_PROMPT,
                include_image=True,
                use_web_search=False,
                stream_callback=_visible_model_stream_callback(
                    progress_callback,
                    stage="scene_assessment",
                    model=self.scene_model,
                ),
            )
        finally:
            backend.cleanup()
        parsed = _parse_json(raw)
        if not parsed:
            parsed = {
                "summary": raw.strip()[:1000],
                "incident_types": ["unknown"],
                "visible_hazards": [],
                "visible_context": [],
                "what_happened": raw.strip()[:1000],
                "confidence": 0.0,
                "uncertainty": "The model did not return structured JSON.",
                "recommended_next_agents": [],
            }
        parsed["raw_model_output"] = raw if os.getenv("GEOSPATIAL_KEEP_RAW_SCENE_OUTPUT") == "1" else ""
        return parsed

    def _maybe_call_peer_agents(
        self,
        *,
        request_text: str,
        location_result: dict[str, Any] | None,
        scene_result: dict[str, Any] | None,
        ads_url: str | None,
    ) -> list[dict[str, Any]]:
        report = _format_analysis_text(
            {
                "location_inference": location_result,
                "scene_assessment": scene_result,
                "peer_results": [],
                "errors": [],
            }
        )
        return _run_peer_agent_tool_loop(
            request_text=request_text,
            analysis_report=report,
            location_result=location_result,
            scene_result=scene_result,
            ads_url=ads_url,
            enable_remote_agent_tools=self.enable_remote_agent_tools,
        )


def _run_peer_agent_tool_loop(
    *,
    request_text: str,
    analysis_report: str,
    location_result: dict[str, Any] | None,
    scene_result: dict[str, Any] | None,
    ads_url: str | None,
    enable_remote_agent_tools: bool | None = None,
) -> list[dict[str, Any]]:
    if not enable_remote_agent_tools:
        _geo_status("Peer coordination skipped", reason="remote_agent_tools_disabled")
        return []
    if not request_text.strip():
        return []
    if not os.getenv("OPENAI_API_KEY"):
        _geo_status("Peer coordination skipped", reason="missing_openai_api_key")
        return []

    try:
        from openai import OpenAI
    except ImportError as exc:
        _geo_status("Peer coordination skipped", reason=f"openai_import_failed: {exc}")
        return []

    model = os.getenv("GEOSPATIAL_COORDINATION_MODEL", DEFAULT_COORDINATION_MODEL)
    max_steps = max(1, min(int(os.getenv("GEOSPATIAL_COORDINATION_MAX_STEPS", "6")), 12))
    client = OpenAI()
    payload = {
        "user_request": request_text,
        "image_analysis_text": analysis_report,
        "location_inference": location_result,
        "scene_assessment": scene_result,
        "instructions": (
            "Decide whether the user's request needs help from other ADS/A2A agents. "
            "If no peer agent is needed, do not call tools. If a peer is needed, first "
            "discover ADS agents, then send an A2A request using an agent_ref from discovery. "
            "For response-plan requests, ask a disaster-response-capable agent to draft the plan "
            "using the image analysis. For explicit email requests, use an email-capable agent and "
            "make sure the email body contains the requested deliverable, such as the drafted plan, "
            "not just the image analysis. Do not invent agents or endpoints."
        ),
    }
    _geo_status("Peer coordination model request started", model=model)
    try:
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are the GeoSpatial Agent coordination planner. You do not analyze images; "
                        "the image has already been analyzed. Use tools only when the user's request "
                        "requires another agent. Use one tool call at a time and stop when the request "
                        "has been delegated or completed."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, indent=2, default=str)},
            ],
            tools=GEOSPATIAL_COORDINATION_TOOL_SCHEMAS,
            parallel_tool_calls=False,
        )
    except Exception as exc:
        _geo_status("Peer coordination model request failed", error=f"{type(exc).__name__}: {exc}")
        return []

    peer_results: list[dict[str, Any]] = []
    for step_index in range(max_steps):
        tool_calls = _extract_function_calls(response)
        if not tool_calls:
            break
        tool_outputs = []
        for call in tool_calls:
            name = _object_get(call, "name", "")
            call_id = _object_get(call, "call_id", "")
            raw_arguments = _object_get(call, "arguments", "{}")
            token_usage = _record_tool_call_token_usage(
                model=model,
                response=response,
                tool=name,
                call_id=call_id,
                step=step_index + 1,
                tool_call_count=len(tool_calls),
            )
            try:
                arguments = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            _geo_status(
                "Peer coordination tool call received",
                tool=name,
                call_id=call_id,
                token_usage=token_usage,
                arguments=arguments,
            )
            result = _run_peer_coordination_tool(name, arguments, ads_url=ads_url)
            if name == "send_a2a_request" and isinstance(result, dict):
                peer_result = {key: value for key, value in result.items() if key != "ok"}
                peer_results.append(peer_result)
            _geo_status(
                "Peer coordination tool call completed",
                tool=name,
                call_id=call_id,
                ok=bool(result.get("ok", False)) if isinstance(result, dict) else False,
                token_usage=token_usage,
                result=result,
            )
            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, default=str),
                }
            )
        try:
            response = client.responses.create(
                model=model,
                input=tool_outputs,
                tools=GEOSPATIAL_COORDINATION_TOOL_SCHEMAS,
                previous_response_id=_object_get(response, "id"),
                parallel_tool_calls=False,
            )
        except Exception as exc:
            _geo_status("Peer coordination follow-up failed", error=f"{type(exc).__name__}: {exc}")
            break

    _geo_status("Peer coordination completed", peer_result_count=len(peer_results))
    return peer_results


def _run_peer_coordination_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    ads_url: str | None,
) -> dict[str, Any]:
    try:
        if name == "discover_ads_agents":
            limit = max(1, min(int(arguments.get("limit") or 10), 50))
            agents = discover_ads_agents(
                skill=str(arguments.get("skill") or ""),
                query=str(arguments.get("query") or ""),
                limit=limit,
                include_self=bool(arguments.get("include_self", False)),
                ads_url=ads_url,
            )
            return {"ok": True, "agents": agents, "count": len(agents)}
        if name == "send_a2a_request":
            timeout_seconds = max(
                1,
                min(
                    int(arguments.get("timeout_seconds") or DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS),
                    600,
                ),
            )
            result = send_a2a_request_to_ads_agent_sync(
                agent_ref=str(arguments.get("agent_ref") or ""),
                skill=str(arguments.get("skill") or ""),
                message=str(arguments.get("message") or ""),
                data=dict(arguments.get("data") or {}),
                timeout_seconds=timeout_seconds,
                ads_url=ads_url,
                confirmed=True,
            )
            return {"ok": True, **result}
        return {"ok": False, "error": f"Unknown tool: {name}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _extract_function_calls(response: Any) -> list[Any]:
    return [
        item
        for item in (_object_get(response, "output", []) or [])
        if _object_get(item, "type") == "function_call"
    ]


def _object_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _emit_geospatial_progress(
    callback: Callable[[dict[str, Any]], None] | None,
    *,
    event: str,
    text: str,
    **details: Any,
) -> None:
    if callback is None:
        return
    payload = {
        "kind": "geospatial_inference",
        "event": event,
        "text": text,
        **{key: value for key, value in details.items() if value is not None},
    }
    try:
        callback(payload)
    except Exception as exc:
        _geo_status("GeoSpatial progress callback failed", error=f"{type(exc).__name__}: {exc}")


def _visible_model_stream_callback(
    callback: Callable[[dict[str, Any]], None] | None,
    *,
    stage: str,
    model: str,
) -> Callable[[str], None] | None:
    if callback is None:
        return None
    buffer = ""

    def emit(text: str, *, final: bool = False) -> None:
        nonlocal buffer
        if text:
            buffer += text
        if not buffer:
            return
        if not final and len(buffer) < 180 and "\n" not in buffer:
            return
        chunk = buffer
        buffer = ""
        _emit_geospatial_progress(
            callback,
            event="model_output_chunk",
            text=chunk,
            kind="geospatial_model_stream",
            stage=stage,
            model=model,
        )

    def on_chunk(text: str) -> None:
        emit(text)

    setattr(on_chunk, "flush", lambda: emit("", final=True))
    setattr(on_chunk, "progress_callback", callback)
    setattr(on_chunk, "stage", stage)
    setattr(on_chunk, "model", model)
    return on_chunk


def _compact_location_result(location: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(location, dict):
        return {}
    return {
        "description": location.get("description"),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "precision_level": location.get("precision_level"),
        "confidence": location.get("confidence"),
        "hypothesis_chain": list(location.get("hypothesis_chain") or [])[:5],
    }


def _compact_scene_result(scene: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(scene, dict):
        return {}
    return {
        "summary": scene.get("summary"),
        "incident_types": list(scene.get("incident_types") or [])[:5],
        "visible_hazards": list(scene.get("visible_hazards") or [])[:8],
        "confidence": scene.get("confidence"),
        "uncertainty": scene.get("uncertainty"),
    }


def _location_progress_text(location: dict[str, Any] | None) -> str:
    if not isinstance(location, dict):
        return "Location inference completed."
    description = str(location.get("description") or "unknown location")
    confidence = location.get("confidence")
    precision = str(location.get("precision_level") or "unknown precision")
    if isinstance(confidence, (int, float)):
        return f"Location inference completed: {description} ({precision}, confidence {confidence:.2f})."
    return f"Location inference completed: {description} ({precision})."


def _scene_progress_text(scene: dict[str, Any] | None) -> str:
    if not isinstance(scene, dict):
        return "Scene assessment completed."
    summary = str(scene.get("summary") or "scene assessment completed").strip()
    incident_types = ", ".join(str(item) for item in (scene.get("incident_types") or [])[:5])
    return f"Scene assessment completed: {summary}" + (f" Incident type(s): {incident_types}." if incident_types else "")


def _format_analysis_text(output: dict[str, Any]) -> str:
    location = output.get("location_inference") if isinstance(output.get("location_inference"), dict) else None
    scene = output.get("scene_assessment") if isinstance(output.get("scene_assessment"), dict) else None
    lines: list[str] = []
    if scene:
        lines.append("Scene assessment:")
        if scene.get("summary"):
            lines.append(f"- Summary: {scene.get('summary')}")
        incident_types = scene.get("incident_types") or []
        if incident_types:
            lines.append(f"- Incident type(s): {', '.join(str(item) for item in incident_types)}")
        if scene.get("what_happened"):
            lines.append(f"- What happened: {scene.get('what_happened')}")
        if scene.get("visible_hazards"):
            lines.append("- Visible hazards: " + "; ".join(str(item) for item in scene.get("visible_hazards") or []))
        if scene.get("uncertainty"):
            lines.append(f"- Uncertainty: {scene.get('uncertainty')}")
        lines.append("")
    if location:
        lines.append("Location inference:")
        lines.append(f"- Description: {location.get('description', 'Unknown')}")
        lines.append(
            f"- Coordinates: ({float(location.get('latitude', 0.0) or 0.0):.6f}, "
            f"{float(location.get('longitude', 0.0) or 0.0):.6f})"
        )
        lines.append(f"- Precision: {location.get('precision_level', 'unknown')}")
        lines.append(f"- Confidence: {float(location.get('confidence', 0.0) or 0.0):.2f}")
        if location.get("rationale"):
            lines.append(f"- Rationale: {location.get('rationale')}")
        chain = location.get("hypothesis_chain") or []
        if chain:
            lines.append("- Hypothesis chain:")
            lines.extend(f"  - {item}" for item in chain[:8])
        lines.append("")
    peer_results = output.get("peer_results") or []
    if peer_results:
        lines.append("Peer agent results:")
        for result in peer_results:
            agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
            lines.append(
                f"- {agent.get('name') or agent.get('agent_id') or 'Remote agent'}: "
                f"{result.get('state') or 'unknown'}"
            )
    errors = output.get("errors") or []
    if errors:
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines).strip() or "No image analysis result was produced."


def _resolve_existing_image_path(value: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    candidates = [Path(raw).expanduser()]
    if not candidates[0].is_absolute():
        candidates.extend([
            Path.cwd() / raw,
            _AGENT_DIR / raw,
            _ATTACK_BUNDLE / raw,
        ])
    for path in candidates:
        if path.exists() and path.is_file():
            return path.resolve()
    return None


@dataclass(frozen=True)
class PublishedRecord:
    record_id: str
    agent_id: str
    backend: str
    backend_ref: str


class GeoSpatialAgentExecutor(_AgentExecutorBase):
    """A2A executor wrapper around GeoSpatialAgent."""

    def __init__(
        self,
        agent: GeoSpatialAgent | None = None,
        enable_remote_agent_tools: bool = False,
    ):
        _require_a2a()
        self.agent = agent or GeoSpatialAgent(
            enable_remote_agent_tools=enable_remote_agent_tools,
        )
        if agent is not None and enable_remote_agent_tools:
            self.agent.enable_remote_agent_tools = True

    async def execute(self, context: A2ARequestContext, event_queue: A2AEventQueue) -> None:
        updater = _task_updater(event_queue, context.task_id, context.context_id)
        skill_hint = _a2a_skill_from_context(context)
        chosen_skill = (
            skill_hint
            if skill_hint in {
                "geospatial/analyze_image",
                "geospatial/location_inference",
                "geospatial/incident_assessment",
                "image/geospatial_analysis",
            }
            else "geospatial/analyze_image"
        )
        payload = _a2a_payload_from_context(context)
        user_text = _a2a_user_text(context)
        request_id = uuid.uuid4().hex[:12]
        outcome = "completed"
        heartbeat_task: asyncio.Task[None] | None = None
        _geo_status(
            "A2A request received",
            request_id=request_id,
            skill_hint=skill_hint,
            chosen_skill=chosen_skill,
            task_id=context.task_id,
            context_id=context.context_id,
            user_text=user_text,
            payload=payload,
        )
        try:
            image_path = _a2a_image_path_from_context(context, payload)
            if not image_path:
                outcome = "input_required"
                await _a2a_requires_input(
                    updater,
                    skill=chosen_skill,
                    artifact_id="geospatial-image-required",
                    artifact_name="geospatial_image_required",
                    text="Please provide an image path or image file for geospatial analysis.",
                    output={
                        "skill": chosen_skill,
                        "requested_skill": skill_hint,
                        "skill_hint": skill_hint,
                        "chosen_skill": None,
                        "status": "input_required",
                        "missing": ["image_path"],
                        "context_id": context.context_id,
                        "task_id": context.task_id,
                    },
                )
                return

            heartbeat_task = await _start_a2a_working_heartbeat(
                updater,
                agent_name="GeoSpatial Agent",
                skill=chosen_skill,
                request_id=request_id,
                env_var="GEOSPATIAL_A2A_HEARTBEAT_SECONDS",
            )
            loop = asyncio.get_running_loop()

            def progress_callback(event: dict[str, Any]) -> None:
                text = str(event.get("text") or "GeoSpatial Agent is working.")
                metadata = {
                    "progress_event": {
                        "agent": "GeoSpatial Agent",
                        "request_id": request_id,
                        **event,
                    }
                }
                future = asyncio.run_coroutine_threadsafe(
                    _a2a_working_update(updater, text=text, metadata=metadata),
                    loop,
                )
                with contextlib.suppress(Exception):
                    future.result(timeout=5)

            result = await _maybe_to_thread(
                self.agent.analyze_image,
                image_path,
                request_text=user_text,
                image_condition=str(payload.get("image_condition") or payload.get("context") or ""),
                skill=chosen_skill,
                ads_url=str(payload.get("ads_url") or "") or None,
                progress_callback=progress_callback,
            )
            result.update(
                {
                    "requested_skill": skill_hint,
                    "skill_hint": skill_hint,
                    "chosen_skill": chosen_skill,
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                }
            )
            await _stop_a2a_working_heartbeat(heartbeat_task)
            heartbeat_task = None
            await _a2a_complete(
                updater,
                skill=chosen_skill,
                artifact_id="geospatial-analysis",
                artifact_name="geospatial_analysis",
                text=result["response_text"],
                output=result,
            )
        except Exception as exc:
            outcome = "error"
            _geo_status(
                "A2A request failed",
                request_id=request_id,
                skill=chosen_skill,
                task_id=context.task_id,
                context_id=context.context_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            await _stop_a2a_working_heartbeat(heartbeat_task)
            heartbeat_task = None
            await _a2a_failed(
                updater,
                skill=chosen_skill,
                artifact_id="geospatial-error",
                artifact_name="geospatial_error",
                text=f"{type(exc).__name__}: {exc}",
                output={
                    "skill": chosen_skill,
                    "requested_skill": skill_hint,
                    "skill_hint": skill_hint,
                    "chosen_skill": None,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
        finally:
            await _stop_a2a_working_heartbeat(heartbeat_task)
            _geo_status(
                "A2A request finished",
                request_id=request_id,
                skill=chosen_skill,
                skill_hint=skill_hint,
                task_id=context.task_id,
                context_id=context.context_id,
                outcome=outcome,
            )

    async def cancel(self, context: A2ARequestContext, event_queue: A2AEventQueue) -> None:
        _geo_status("A2A request cancelled", task_id=context.task_id, context_id=context.context_id)
        updater = _task_updater(event_queue, context.task_id, context.context_id)
        await updater.reject()


async def _maybe_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("Cannot run a remote A2A request from an active event loop.")


def load_geospatial_a2a_agent_card(card_path: Path | str = DEFAULT_CARD_PATH) -> A2AAgentCard:
    _require_a2a()
    assert _AgentCard is not None
    with Path(card_path).open("r", encoding="utf-8") as fh:
        return _AgentCard.model_validate(json.load(fh))


def create_geospatial_a2a_app(
    agent_card: A2AAgentCard,
    agent: GeoSpatialAgent | None = None,
    enable_remote_agent_tools: bool = False,
) -> Any:
    _require_a2a()
    assert _A2AStarletteApplication is not None
    assert _DefaultRequestHandler is not None
    assert _InMemoryTaskStore is not None
    request_handler = _DefaultRequestHandler(
        agent_executor=GeoSpatialAgentExecutor(
            agent,
            enable_remote_agent_tools=enable_remote_agent_tools,
        ),
        task_store=_InMemoryTaskStore(),
    )
    return _A2AStarletteApplication(agent_card=agent_card, http_handler=request_handler).build()


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
        "auth": {"security": getattr(agent_card, "security", None) or [], "security_schemes": {}, "required_scopes": []},
        "policy": {"risk_level": "low", "action_type": "read"},
        "metadata": {"source": "standalone_geospatial_agent", "demo_specific_custom_skills": custom_tags},
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
        "name": provider_url.rstrip("/") if provider_url.startswith(("http://", "https://")) else "https://example.invalid/agents/geospatial-agent",
        "version": getattr(agent_card, "version", ""),
        "authors": [base_record.get("organization") or "GeoSpatial Agent"],
        "description": getattr(agent_card, "description", ""),
        "created_at": created_at,
        "skills": OFFICIAL_ADS_ROUTING_SKILLS,
        # Real ADS expects typed Domain objects here; the demo agents leave
        # this empty and preserve searchable labels in skill tags/annotations.
        "domains": [],
        "locators": [
            {
                "type": "source_code",
                "urls": [provider_url or getattr(agent_card, "url", "")],
                "annotations": {"a2a.url": getattr(agent_card, "url", "")},
            }
        ],
        "modules": [{"name": "integration/a2a", "data": {"card_schema_version": "0.3.0", "card_data": card_data}}],
        "annotations": {
            "a2a.url": getattr(agent_card, "url", ""),
            "agentic_network.agent_id": base_record["agent_id"],
            "agentic_network.record_id": base_record["record_id"],
            "agentic_network.display_name": getattr(agent_card, "name", ""),
            "agentic_network.organization": base_record.get("organization", "unknown"),
            "agentic_network.custom_skill_tags": ",".join(custom_tags),
        },
    }


def publish_geospatial_a2a_agent_card_to_ads(
    agent_card: A2AAgentCard,
    *,
    ads_url: str | None = None,
    ads_backend: str | None = DEFAULT_ADS_BACKEND,
) -> PublishedRecord:
    _geo_status(
        "Publishing GeoSpatial Agent card to ADS",
        ads_url=ads_url or "env",
        ads_backend=ads_backend or "env",
        agent_url=getattr(agent_card, "url", ""),
    )
    backend = (ads_backend or os.getenv("ADS_BACKEND") or DEFAULT_ADS_BACKEND).lower()
    if backend != "cli":
        raise RuntimeError(
            "Standalone GeoSpatial Agent ADS publication supports ADS_BACKEND=cli only. "
            "Use --ads-backend cli or set ADS_BACKEND=cli."
        )
    ads_record = _agent_card_to_ads_record(agent_card)
    internal_record = _agent_card_to_oasf_record(agent_card)
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as fh:
        json.dump(ads_record, fh, indent=2, sort_keys=True)
        temp_path = Path(fh.name)
    try:
        _geo_status("Pushing GeoSpatial Agent ADS record", record_file=str(temp_path))
        push = _run_dirctl(["push", str(temp_path), "--output", "raw"], ads_url=ads_url)
        cid = push.stdout.strip()
        if not cid:
            raise RuntimeError("dirctl push did not return a CID.")
        _geo_status("Publishing GeoSpatial Agent ADS routing entry", cid=cid)
        _run_dirctl(["routing", "publish", cid], ads_url=ads_url)
    finally:
        temp_path.unlink(missing_ok=True)
    published = PublishedRecord(
        record_id=internal_record["record_id"],
        agent_id=internal_record["agent_id"],
        backend="real",
        backend_ref=cid,
    )
    _geo_status("GeoSpatial Agent card published to ADS", backend=published.backend, record=cid)
    return published


def unpublish_geospatial_a2a_agent_card_from_ads(
    published: PublishedRecord | str | None,
    *,
    ads_url: str | None = None,
) -> bool:
    if published is None:
        return False
    cid = published if isinstance(published, str) else published.backend_ref
    if not cid:
        _geo_status("Skipping ADS unpublish", reason="missing published CID")
        return False

    _geo_status("Unpublishing GeoSpatial Agent from ADS", cid=cid)
    try:
        _run_dirctl(["routing", "unpublish", cid], ads_url=ads_url)
    except Exception as exc:
        _geo_status("Failed to unpublish GeoSpatial Agent from ADS", cid=cid, error=str(exc))
        return False

    _geo_status("GeoSpatial Agent unpublished from ADS", cid=cid)
    return True


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


def _try_dirctl(args: list[str], *, ads_url: str | None, errors: list[str]) -> subprocess.CompletedProcess[str] | None:
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


def _a2a_url_from_ads_record(record: dict[str, Any], card_data: dict[str, Any] | None) -> str:
    annotations = record.get("annotations") if isinstance(record.get("annotations"), dict) else {}
    url = annotations.get("a2a.url")
    if isinstance(url, str) and url:
        return url
    interaction = record.get("interaction")
    if isinstance(interaction, dict) and isinstance(interaction.get("endpoint"), str):
        return interaction["endpoint"]
    if card_data and isinstance(card_data.get("url"), str):
        return card_data["url"]
    return ""


def _ads_record_to_agent_summary(record: dict[str, Any], *, cid: str | None = None) -> dict[str, Any]:
    card_data = _card_data_from_ads_record(record) or {}
    annotations = record.get("annotations") if isinstance(record.get("annotations"), dict) else {}
    endpoint = _a2a_url_from_ads_record(record, card_data)
    name = str(card_data.get("name") or annotations.get("agentic_network.display_name") or record.get("name") or "Unknown Agent")
    provider = card_data.get("provider") if isinstance(card_data.get("provider"), dict) else {}
    skills = _skill_summaries_from_card_data(card_data)
    custom_tags = _custom_skill_tags_from_ads_record(record, skills)
    agent_id = str(annotations.get("agentic_network.agent_id") or record.get("agent_id") or name.lower().replace(" ", "-"))
    record_id = str(annotations.get("agentic_network.record_id") or record.get("record_id") or cid or agent_id)
    return {
        "agent_id": agent_id,
        "record_id": record_id,
        "cid": cid or "",
        "name": name,
        "organization": str(annotations.get("agentic_network.organization") or record.get("organization") or provider.get("organization") or "unknown"),
        "description": str(card_data.get("description") or record.get("description") or ""),
        "endpoint": endpoint,
        "source_card_url": f"{endpoint.rstrip('/')}/.well-known/agent-card.json" if endpoint else "",
        "skills": skills,
        "custom_skill_tags": custom_tags,
        "official_routing_skills": [item for item in record.get("skills", []) or [] if isinstance(item, dict)],
    }


def _skill_summaries_from_card_data(card_data: dict[str, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for skill in card_data.get("skills", []) or []:
        if isinstance(skill, dict):
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


def _custom_skill_tags_from_ads_record(record: dict[str, Any], skills: list[dict[str, Any]]) -> list[str]:
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


def _ads_skill_hints(skill: str) -> list[str]:
    if not skill:
        return []
    if skill.startswith(("geospatial/", "image/")):
        return [str(item["name"]) for item in OFFICIAL_ADS_ROUTING_SKILLS]
    if skill.startswith(("disaster/", "email/")):
        return [
            "natural_language_processing/natural_language_generation/summarization",
            "natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search",
        ]
    return []


def _pull_ads_record(cid: str, *, ads_url: str | None = None) -> dict[str, Any]:
    result = _run_dirctl(["pull", cid, "--output", "json"], ads_url=ads_url)
    try:
        return _record_from_ads_pull_output(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"dirctl pull {cid} did not return a valid ADS record.") from exc


def _ads_search_cids(*, skill: str, limit: int, ads_url: str | None = None) -> list[str]:
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
    else:
        collect(["routing", "search", "--limit", str(limit), "--output", "json"])
        if not cids:
            collect(["routing", "list", "--output", "json"])
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
    haystack = " ".join(str(part).lower() for part in haystack_parts)
    if skill and skill.lower() not in haystack:
        return False
    if query and query.lower() not in haystack:
        return False
    return True


def _current_agent_urls() -> set[str]:
    urls = {DEFAULT_AGENT_URL.rstrip("/")}
    env_url = os.getenv("GEOSPATIAL_AGENT_A2A_URL")
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
    agent_id = str(summary.get("agent_id") or "").lower()
    if agent_id in {"geospatial-agent", "geo-spatial-agent", "photo-geospatial-agent"}:
        return True
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
    summaries: list[dict[str, Any]] = []
    for cid in _ads_search_cids(skill=skill, limit=max(limit * 3, limit), ads_url=ads_url):
        try:
            record = _pull_ads_record(cid, ads_url=ads_url)
            summary = _ads_record_to_agent_summary(record, cid=cid)
        except Exception as exc:
            _geo_status("Skipping ADS record", cid=cid, error=str(exc))
            continue
        if not include_self and _is_current_agent_summary(summary):
            continue
        if _agent_summary_matches(summary, skill=skill, query=query):
            summaries.append(summary)
        if len(summaries) >= limit:
            break
    return summaries


async def send_a2a_request_to_ads_agent(
    *,
    agent_ref: str,
    skill: str = "",
    message: str = "",
    data: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS,
    ads_url: str | None = None,
    confirmed: bool = True,
) -> dict[str, Any]:
    skill_hint = skill.strip() or None
    if not message.strip():
        raise ValueError("message is required.")
    _, summary, card_data = _resolve_ads_agent(agent_ref, ads_url=ads_url)
    if _is_current_agent_summary(summary):
        raise RuntimeError("Refusing to send a remote A2A request to this same geospatial agent.")
    try:
        import httpx
        from a2a.client import ClientConfig as SdkClientConfig
        from a2a.client import ClientFactory as SdkClientFactory
        from a2a.types import AgentCard, DataPart, Message, Part, Role, TextPart, TransportProtocol
    except ImportError as exc:
        raise RuntimeError("Remote A2A requests require the a2a-sdk and httpx packages.") from exc

    card = AgentCard.model_validate(card_data)
    context_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    part_metadata = {"skill": skill_hint, "requested_skill": skill_hint, "skill_hint": skill_hint} if skill_hint else {}
    parts = [Part(root=TextPart(text=message, metadata=part_metadata or None))]
    if data:
        parts.append(Part(root=DataPart(data=data, metadata=part_metadata or None)))
    message_metadata = {
        "target_agent_id": summary.get("agent_id"),
        "requested_task_id": task_id,
        "user_goal": message,
        "confirmed": bool(confirmed),
    }
    if skill_hint:
        message_metadata.update({"skill": skill_hint, "requested_skill": skill_hint, "skill_hint": skill_hint})
    _geo_status("Sending remote A2A request", agent=summary.get("agent_id"), skill_hint=skill_hint, endpoint=summary.get("endpoint"))
    sdk_message = Message(
        role=Role.user,
        message_id=str(uuid.uuid4()),
        context_id=context_id,
        task_id=None,
        parts=parts,
        metadata=message_metadata,
    )
    async with httpx.AsyncClient(timeout=timeout_seconds) as http_client:
        client_config = SdkClientConfig(
            streaming=True,
            polling=False,
            httpx_client=http_client,
            supported_transports=[TransportProtocol.jsonrpc],
            use_client_preference=True,
            accepted_output_modes=list(getattr(card, "default_output_modes", None) or []),
        )
        sdk_client = SdkClientFactory(client_config).create(card)
        response = None
        async for event in sdk_client.send_message(sdk_message):
            _geo_status("Remote A2A stream status", agent=summary.get("agent_id"), skill_hint=skill_hint, **_a2a_stream_event_status(event))
            response = event
        if response is None:
            raise RuntimeError("A2A request completed without a response event.")
    result = _summarize_a2a_response(response)
    result["agent"] = {key: summary.get(key) for key in ("agent_id", "record_id", "cid", "name", "organization", "endpoint")}
    result["skill"] = skill_hint
    result["requested_skill"] = skill_hint
    result["skill_hint"] = skill_hint
    result["confirmed"] = confirmed
    _geo_status("Remote A2A request completed", agent=summary.get("agent_id"), skill_hint=skill_hint, state=result.get("state"))
    return result


def send_a2a_request_to_ads_agent_sync(**kwargs: Any) -> dict[str, Any]:
    return _run_async(send_a2a_request_to_ads_agent(**kwargs))


def _resolve_ads_agent(agent_ref: str, *, ads_url: str | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not agent_ref:
        raise ValueError("agent_ref is required.")
    candidates = discover_ads_agents(limit=50, include_self=True, ads_url=ads_url)
    for summary in candidates:
        identifiers = {
            str(summary.get("cid") or ""),
            str(summary.get("record_id") or ""),
            str(summary.get("agent_id") or ""),
            str(summary.get("name") or ""),
            str(summary.get("endpoint") or "").rstrip("/"),
        }
        if agent_ref in identifiers or agent_ref.rstrip("/") in identifiers:
            cid = str(summary.get("cid") or "")
            record = _pull_ads_record(cid, ads_url=ads_url) if cid else {}
            card_data = _card_data_from_ads_record(record)
            if not card_data:
                raise RuntimeError(f"ADS record for {agent_ref!r} does not contain an embedded A2A Agent Card.")
            return record, summary, card_data
    raise KeyError(f"Agent not found in ADS: {agent_ref}")


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
    state = _a2a_value(getattr(status, "state", None)) or ("completed" if hasattr(result, "parts") else "unknown")
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
    return {
        "state": state,
        "task_id": getattr(result, "id", None) or getattr(result, "task_id", None),
        "context_id": getattr(result, "context_id", None),
        "text": "\n".join(text_parts),
        "messages": messages,
        "artifacts": artifacts,
        "structured_output": structured_output,
    }


def _a2a_stream_event_status(event: Any) -> dict[str, Any]:
    task = event
    update = None
    if isinstance(event, tuple) and event:
        task = event[0]
        update = event[1] if len(event) > 1 else None
    status = getattr(update, "status", None) or getattr(task, "status", None)
    state = _a2a_value(getattr(status, "state", None))
    task_id = getattr(update, "task_id", None) or getattr(update, "taskId", None) or getattr(task, "id", None)
    context_id = getattr(update, "context_id", None) or getattr(update, "contextId", None) or getattr(task, "context_id", None) or getattr(task, "contextId", None)
    final = getattr(update, "final", None)
    if final is None:
        final = state in {"completed", "failed", "canceled", "rejected", "input-required", "auth-required"}
    return {"task_id": task_id, "context_id": context_id, "state": state or None, "final": bool(final)}


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
        return {"kind": "text", "content_type": "text/plain", "text": str(getattr(root, "text", "")), "metadata": metadata}
    if hasattr(root, "data"):
        return {"kind": "data", "content_type": "application/json", "data": getattr(root, "data", None) or {}, "metadata": metadata}
    file_obj = getattr(root, "file", None)
    if file_obj is not None:
        return {
            "kind": "file",
            "content_type": getattr(file_obj, "mime_type", None) or getattr(file_obj, "mimeType", None) or "application/octet-stream",
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


def _a2a_value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _require_a2a() -> None:
    if A2A_IMPORT_ERROR is not None:
        raise RuntimeError("A2A server mode requires a2a-sdk, starlette, and uvicorn.") from A2A_IMPORT_ERROR


def _task_updater(event_queue: A2AEventQueue, task_id: str, context_id: str) -> A2ATaskUpdater:
    _require_a2a()
    assert _TaskUpdater is not None
    return _TaskUpdater(event_queue, task_id, context_id)


def _heartbeat_interval_seconds(env_var: str, default: float = 20.0) -> float:
    raw = os.getenv(env_var)
    if raw is None:
        return default
    try:
        return max(0.1, float(raw))
    except ValueError:
        return default


async def _start_a2a_working_heartbeat(
    updater: A2ATaskUpdater,
    *,
    agent_name: str,
    skill: str,
    request_id: str,
    env_var: str,
) -> asyncio.Task[None]:
    interval = _heartbeat_interval_seconds(env_var)
    started = time.monotonic()

    async def emit() -> None:
        elapsed_s = round(time.monotonic() - started, 1)
        await _a2a_working_update(
            updater,
            text=f"{agent_name} is working on {skill}.",
            metadata={"progress_event": {"kind": "heartbeat", "agent": agent_name, "skill": skill, "request_id": request_id, "elapsed_s": elapsed_s}},
        )

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(interval)
            await emit()

    await emit()
    return asyncio.create_task(heartbeat())


async def _stop_a2a_working_heartbeat(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _a2a_working_update(updater: A2ATaskUpdater, *, text: str, metadata: dict[str, Any]) -> None:
    from a2a.types import TaskState

    message = updater.new_agent_message([_text_part(text)], metadata=metadata)
    await updater.update_status(TaskState.working, message=message, final=False)


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


def _a2a_skill_from_context(context: A2ARequestContext) -> str | None:
    for metadata in _a2a_metadata_sources(context):
        skill = metadata.get("skill") or metadata.get("requested_skill") or metadata.get("skill_hint")
        if skill:
            return str(skill)
    return None


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


def _a2a_image_path_from_context(context: A2ARequestContext, payload: dict[str, Any]) -> str:
    for key in ("image_path", "local_image_path", "image", "file_path", "path"):
        value = payload.get(key)
        if isinstance(value, str):
            path = _resolve_existing_image_path(value)
            if path:
                return str(path)
    if isinstance(payload.get("image_b64"), str):
        return str(_save_image_bytes(base64.b64decode(payload["image_b64"]), name="payload_image.jpg"))
    if context.message:
        for part in context.message.parts:
            root = part.root
            file_obj = getattr(root, "file", None)
            if file_obj is None:
                continue
            uri = getattr(file_obj, "uri", None)
            if isinstance(uri, str):
                path = _resolve_existing_image_path(uri)
                if path:
                    return str(path)
            raw_bytes = getattr(file_obj, "bytes", None)
            if isinstance(raw_bytes, str) and raw_bytes:
                data = base64.b64decode(raw_bytes)
                name = getattr(file_obj, "name", None) or "a2a_image.jpg"
                return str(_save_image_bytes(data, name=name))
    text = _a2a_user_text(context)
    match = re.search(r"(?i)\b(?:image|photo|file|path)\s*:\s*(\S+)", text)
    if match:
        path = _resolve_existing_image_path(match.group(1))
        if path:
            return str(path)
    return ""


def _save_image_bytes(data: bytes, *, name: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or "a2a_image.jpg"
    out_dir = Path(tempfile.gettempdir()) / "geospatial_agent_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{uuid.uuid4().hex}_{safe_name}"
    path.write_bytes(data)
    return path


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
    await updater.add_artifact([_data_part(output)], artifact_id=artifact_id, name=artifact_name, metadata={"skill": skill, "content_type": "application/json"})
    await updater.complete(message=updater.new_agent_message([_text_part(text)], metadata={"structured_output": output}))


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
    await updater.add_artifact([_data_part(output)], artifact_id=artifact_id, name=artifact_name, metadata={"skill": skill, "content_type": "application/json"})
    await updater.failed(message=updater.new_agent_message([_text_part(text)], metadata={"structured_output": output}))


async def _a2a_requires_input(
    updater: A2ATaskUpdater,
    *,
    skill: str,
    artifact_id: str,
    artifact_name: str,
    text: str,
    output: dict[str, Any],
) -> None:
    await updater.add_artifact([_data_part(output)], artifact_id=artifact_id, name=artifact_name, metadata={"skill": skill, "content_type": "application/json"})
    await updater.requires_input(message=updater.new_agent_message([_text_part(text)], metadata={"structured_output": output}), final=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GeoSpatial image analysis agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze and geolocate an image.")
    analyze_parser.add_argument("--image", required=True)
    analyze_parser.add_argument("--geo-model", default=os.getenv("GEOSPATIAL_GEO_MODEL", DEFAULT_GEO_MODEL))
    analyze_parser.add_argument("--scene-model", default=os.getenv("GEOSPATIAL_SCENE_MODEL", DEFAULT_SCENE_MODEL))
    analyze_parser.add_argument("--image-condition", default="")
    analyze_parser.add_argument("--request", default="")
    analyze_parser.add_argument("--max-image-dim", type=int, default=0)
    analyze_parser.add_argument(
        "--enable-remote-agent-tools",
        action="store_true",
        help="Allow the analysis flow to use ADS discovery and outbound A2A request tools.",
    )
    analyze_parser.add_argument("--json", action="store_true")

    discover_parser = subparsers.add_parser("ads-discover", help="Discover agents from ADS.")
    discover_parser.add_argument("--skill", default="")
    discover_parser.add_argument("--query", default="")
    discover_parser.add_argument("--limit", type=int, default=10)
    discover_parser.add_argument("--include-self", action="store_true")
    discover_parser.add_argument("--ads-url", default=_default_ads_url())

    send_parser = subparsers.add_parser("a2a-send", help="Send an A2A request to an ADS-discovered agent.")
    send_parser.add_argument("agent_ref")
    send_parser.add_argument("--skill", default="")
    send_parser.add_argument("--message", required=True)
    send_parser.add_argument("--data-json", default="{}")
    send_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS)
    send_parser.add_argument("--ads-url", default=_default_ads_url())

    serve_parser = subparsers.add_parser("serve-a2a", help="Run as an A2A HTTP server.")
    serve_parser.add_argument("--geo-model", default=os.getenv("GEOSPATIAL_GEO_MODEL", DEFAULT_GEO_MODEL))
    serve_parser.add_argument("--scene-model", default=os.getenv("GEOSPATIAL_SCENE_MODEL", DEFAULT_SCENE_MODEL))
    serve_parser.add_argument("--host", default=os.getenv("GEOSPATIAL_AGENT_A2A_HOST", DEFAULT_HOST))
    serve_parser.add_argument("--port", type=int, default=int(os.getenv("GEOSPATIAL_AGENT_A2A_PORT", str(DEFAULT_PORT))))
    serve_parser.add_argument("--card", type=Path, default=DEFAULT_CARD_PATH)
    serve_parser.add_argument("--advertise-url", default=os.getenv("GEOSPATIAL_AGENT_A2A_URL"))
    serve_parser.add_argument(
        "--enable-remote-agent-tools",
        action="store_true",
        help="Allow served requests to use ADS discovery and outbound A2A request tools.",
    )
    serve_parser.add_argument("--ads-url", default=_default_ads_url())
    serve_parser.add_argument("--ads-backend", choices=["cli"], default=DEFAULT_ADS_BACKEND)
    serve_parser.add_argument("--no-publish-ads", action="store_true")

    return parser


def _default_ads_url() -> str:
    return (
        os.getenv("ADS_SERVER_ADDR")
        or os.getenv("ADS_URL")
        or os.getenv("ADS_SERVER_ADDRESS")
        or os.getenv("DIRECTORY_CLIENT_SERVER_ADDRESS")
        or DEFAULT_ADS_SERVER_ADDR
    )


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "analyze":
        agent = GeoSpatialAgent(
            geo_model=args.geo_model,
            scene_model=args.scene_model,
            max_image_dim=args.max_image_dim,
            enable_remote_agent_tools=args.enable_remote_agent_tools,
        )
        result = agent.analyze_image(args.image, request_text=args.request, image_condition=args.image_condition)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result["response_text"])
        return

    if args.command == "ads-discover":
        agents = discover_ads_agents(skill=args.skill, query=args.query, limit=args.limit, include_self=args.include_self, ads_url=args.ads_url)
        print(json.dumps({"agents": agents}, indent=2))
        return

    if args.command == "a2a-send":
        data = json.loads(args.data_json)
        result = send_a2a_request_to_ads_agent_sync(
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
        import uvicorn

        card = _with_advertised_url(load_geospatial_a2a_agent_card(args.card), args.advertise_url)
        published: PublishedRecord | None = None
        if not args.no_publish_ads:
            published = publish_geospatial_a2a_agent_card_to_ads(card, ads_url=args.ads_url, ads_backend=args.ads_backend)
            _geo_status("GeoSpatial Agent published to ADS", cid=published.backend_ref)
        app = create_geospatial_a2a_app(
            card,
            GeoSpatialAgent(
                geo_model=args.geo_model,
                scene_model=args.scene_model,
                enable_remote_agent_tools=args.enable_remote_agent_tools,
            ),
            enable_remote_agent_tools=args.enable_remote_agent_tools,
        )
        _geo_status("Starting GeoSpatial Agent A2A server", host=args.host, port=args.port, url=getattr(card, "url", ""))
        try:
            uvicorn.run(app, host=args.host, port=args.port, log_level=os.getenv("GEOSPATIAL_UVICORN_LOG_LEVEL", "info"))
        finally:
            if published is not None and os.getenv("GEOSPATIAL_AGENT_UNPUBLISH_ON_EXIT", "0") == "1":
                unpublish_geospatial_a2a_agent_card_from_ads(published, ads_url=args.ads_url)
        return


if __name__ == "__main__":
    main()
