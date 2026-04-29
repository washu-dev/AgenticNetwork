from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from v2_agent_network.services.pi_agent.llm_config import PILLMConfig
from v2_agent_network.services.pi_agent.skill_catalog import AvailableSkill

logger = logging.getLogger(__name__)


FORBIDDEN_KEYS = {"endpoint", "url", "agent_url", "base_url", "credentials", "api_key"}


class PILLMPlannerError(RuntimeError):
    """Raised when the LLM planner fails or returns unsafe output."""


class PIPlannerStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    reason: str
    confidence: float = Field(..., ge=0, le=1)
    risk_hint: str
    requires_confirmation: bool
    required_inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("skill")
    @classmethod
    def reject_endpoint_skill(cls, value: str) -> str:
        if _looks_like_url(value):
            raise ValueError("Planner skill must be a skill id, not a URL.")
        return value


class PIPlannerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: list[PIPlannerStep] = Field(default_factory=list)
    needs_clarification: bool = False
    clarifying_question: str | None = None
    user_visible_plan: str = ""

    @model_validator(mode="before")
    @classmethod
    def reject_forbidden_content(cls, value: Any) -> Any:
        _reject_forbidden(value)
        return value


class PILLMPlanner:
    def __init__(self, config: PILLMConfig, openai_client: Any | None = None):
        self.config = config
        self._openai_client = openai_client

    async def plan(
        self,
        *,
        user_text: str,
        requested_skill: str | None,
        available_skills: list[AvailableSkill],
        conversation_context: dict[str, Any] | None = None,
    ) -> PIPlannerPlan:
        available_skill_ids = {item.skill for item in available_skills}
        logger.info(
            "llm_planner_start model=%s requested_skill=%s user_text_chars=%d available_skill_count=%d",
            self.config.model,
            requested_skill,
            len(user_text),
            len(available_skills),
        )
        if requested_skill and requested_skill not in available_skill_ids:
            logger.warning(
                "llm_planner_requested_skill_unavailable requested_skill=%s available_skill_count=%d",
                requested_skill,
                len(available_skills),
            )
            return PIPlannerPlan(
                workflow=[],
                needs_clarification=True,
                clarifying_question=f"No capable agent found for requested_skill {requested_skill}.",
                user_visible_plan="No capable agent was discovered for the requested skill.",
            )
        raw_plan = await self._call_openai(
            user_text=user_text,
            requested_skill=requested_skill,
            available_skills=available_skills,
            conversation_context=conversation_context or {},
        )
        plan = _parse_plan(raw_plan)
        _validate_planned_skills(
            plan=plan,
            available_skill_ids=available_skill_ids,
            requested_skill=requested_skill,
        )
        logger.info(
            "llm_planner_done model=%s workflow=%s needs_clarification=%s",
            self.config.model,
            [step.skill for step in plan.workflow],
            plan.needs_clarification,
        )
        return plan

    async def synthesize_response(
        self,
        *,
        user_text: str,
        conversation_context: dict[str, Any],
        workflow: list[str],
        selected_agents: list[dict[str, Any]],
        structured_outputs: dict[str, Any],
        source_artifacts: list[dict[str, Any]],
        fallback_text: str,
    ) -> str:
        logger.info(
            "llm_synthesis_start model=%s workflow=%s structured_output_keys=%s",
            self.config.model,
            workflow,
            sorted(structured_outputs.keys()),
        )
        payload = {
            "user_text": user_text,
            "conversation_context": conversation_context,
            "workflow": workflow,
            "selected_agents": selected_agents,
            "structured_outputs": structured_outputs,
            "source_artifacts": source_artifacts,
            "fallback_text": fallback_text,
        }
        client = self._openai_client or _default_openai_client(self.config)
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "input": [
                {"role": "system", "content": _synthesis_prompt()},
                {"role": "user", "content": json.dumps(payload, sort_keys=True, default=str)},
            ],
            "max_output_tokens": min(self.config.max_output_tokens, 1200),
        }
        if self.config.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.config.reasoning_effort}
        text_config: dict[str, Any] = {}
        if self.config.verbosity:
            text_config["verbosity"] = self.config.verbosity
        if text_config:
            kwargs["text"] = text_config
        try:
            response = await client.responses.create(**kwargs)
        except TypeError:
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("text", None)
            response = await client.responses.create(**fallback_kwargs)
        except Exception as exc:
            logger.exception("llm_synthesis_openai_request_failed model=%s", self.config.model)
            raise PILLMPlannerError(f"OpenAI synthesis request failed: {type(exc).__name__}: {exc}") from exc
        synthesized = _response_text(response).strip()
        if not synthesized:
            return fallback_text
        logger.info("llm_synthesis_done model=%s chars=%d", self.config.model, len(synthesized))
        return synthesized

    async def _call_openai(
        self,
        *,
        user_text: str,
        requested_skill: str | None,
        available_skills: list[AvailableSkill],
        conversation_context: dict[str, Any],
    ) -> str:
        client = self._openai_client or _default_openai_client(self.config)
        payload = {
            "user_text": user_text,
            "requested_skill": requested_skill,
            "conversation_context": conversation_context,
            "available_skills": [item.model_dump(mode="json") for item in available_skills],
        }
        text_config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "name": "pi_planner_plan",
                "schema": _openai_response_schema(),
                "strict": True,
            }
        }
        if self.config.verbosity:
            text_config["verbosity"] = self.config.verbosity
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "input": [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": json.dumps(payload, sort_keys=True)},
            ],
            "max_output_tokens": self.config.max_output_tokens,
            "text": text_config,
        }
        if self.config.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.config.reasoning_effort}
        try:
            response = await client.responses.create(**kwargs)
        except TypeError:
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["text"] = {"format": text_config["format"]}
            try:
                response = await client.responses.create(**fallback_kwargs)
            except TypeError:
                fallback_kwargs.pop("text", None)
                response = await client.responses.create(**fallback_kwargs)
        except Exception as exc:
            logger.exception("llm_planner_openai_request_failed model=%s", self.config.model)
            raise PILLMPlannerError(f"OpenAI planner request failed: {type(exc).__name__}: {exc}") from exc
        return _response_text(response)


def _default_openai_client(config: PILLMConfig) -> Any:
    try:
        from openai import AsyncOpenAI
    except Exception as exc:
        raise PILLMPlannerError(f"OpenAI SDK import failed: {type(exc).__name__}: {exc}") from exc
    return AsyncOpenAI(timeout=config.timeout_seconds)


def _system_prompt() -> str:
    return (
        "You are the PI Agent planner. Return only strict JSON matching the provided schema. "
        "Choose workflow skill ids only from available_skills. Never output endpoint URLs, "
        "raw HTTP destinations, credentials, or API keys. Directory discovery and policy "
        "approval happen outside you; do not approve write actions. Use required_inputs to "
        "normalize safe task arguments. For email/draft and email/send, extract recipient "
        "email addresses into to, write a concise subject, and write the requested email body. "
        "Set create_gmail_draft to false unless the user explicitly asks to create a Gmail "
        "draft. Never set confirmed to true; email/send must wait for external confirmation. "
        "Use conversation_context to resolve follow-up requests. If the user asks about a "
        "reply, response, result, previous message, previous artifact, or 'it/that', inspect "
        "conversation_history, recent_invocations, previous_outputs, and source_artifacts before "
        "asking a clarifying question. For a follow-up asking whether an email got a response, "
        "prefer an available read-only email search/read/summarize skill using the prior sent "
        "email recipient, subject, and task references."
    )


def _synthesis_prompt() -> str:
    return (
        "You are the PI Agent response synthesizer. Use only the provided tool/A2A "
        "outputs, artifacts, and conversation context to answer the user's latest message. "
        "Do not claim an email, calendar event, document, sheet, or repository change happened "
        "unless the structured outputs explicitly show it. If the user asks whether there was "
        "a response to a sent email, inspect email/search and email/read outputs and summarize "
        "the relevant messages. If the outputs contain only counts or insufficient detail, say "
        "what was found and what is still missing. Be concise and user-facing. Never expose "
        "credentials, endpoint URLs, raw internal JSON, or API keys."
    )


def _parse_plan(raw_plan: str) -> PIPlannerPlan:
    try:
        data = json.loads(raw_plan)
    except json.JSONDecodeError as exc:
        raise PILLMPlannerError(f"LLM planner returned non-JSON output: {exc}") from exc
    try:
        return PIPlannerPlan.model_validate(data)
    except Exception as exc:
        raise PILLMPlannerError(f"LLM planner output failed validation: {exc}") from exc


def _validate_planned_skills(
    *,
    plan: PIPlannerPlan,
    available_skill_ids: set[str],
    requested_skill: str | None,
) -> None:
    for step in plan.workflow:
        if step.skill not in available_skill_ids:
            raise PILLMPlannerError(
                f"LLM planner selected unavailable skill {step.skill!r}; discovery did not return it."
            )
        if requested_skill and step.skill != requested_skill:
            raise PILLMPlannerError(
                f"LLM planner selected {step.skill!r}, but requested_skill was {requested_skill!r}."
            )


def _response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)
    if isinstance(response, dict):
        if response.get("output_text"):
            return str(response["output_text"])
        output = response.get("output")
    else:
        output = getattr(response, "output", None)
    if output:
        chunks: list[str] = []
        for item in output:
            content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
            for part in content or []:
                text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if text:
                    chunks.append(str(text))
        if chunks:
            return "".join(chunks)
    raise PILLMPlannerError("OpenAI planner response did not include output text.")


def _openai_response_schema() -> dict[str, Any]:
    """Return an OpenAI strict schema without refs or dynamic object maps."""

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workflow": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "skill": {"type": "string"},
                        "reason": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "risk_hint": {"type": "string"},
                        "requires_confirmation": {"type": "boolean"},
                        "required_inputs": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": _required_inputs_properties_schema(),
                            "required": list(_required_inputs_properties_schema().keys()),
                        },
                    },
                    "required": [
                        "skill",
                        "reason",
                        "confidence",
                        "risk_hint",
                        "requires_confirmation",
                        "required_inputs",
                    ],
                },
            },
            "needs_clarification": {"type": "boolean"},
            "clarifying_question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "user_visible_plan": {"type": "string"},
        },
        "required": [
            "workflow",
            "needs_clarification",
            "clarifying_question",
            "user_visible_plan",
        ],
    }


def _required_inputs_properties_schema() -> dict[str, Any]:
    string_or_null = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    bool_or_null = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
    string_array_or_null = {
        "anyOf": [
            {"type": "array", "items": {"type": "string"}},
            {"type": "null"},
        ]
    }
    return {
        "to": string_array_or_null,
        "cc": string_array_or_null,
        "bcc": string_array_or_null,
        "recipient_name": string_or_null,
        "subject": string_or_null,
        "body": string_or_null,
        "create_gmail_draft": bool_or_null,
        "confirmed": bool_or_null,
    }


def _openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Deprecated compatibility wrapper for tests and callers."""

    return _openai_response_schema()


def _strip_defaults(value: Any) -> None:
    if isinstance(value, dict):
        value.pop("default", None)
        for item in value.values():
            _strip_defaults(item)
    elif isinstance(value, list):
        for item in value:
            _strip_defaults(item)


def _enforce_strict_objects(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["required"] = list(properties.keys())
            value["additionalProperties"] = False
        for item in value.values():
            _enforce_strict_objects(item)
    elif isinstance(value, list):
        for item in value:
            _enforce_strict_objects(item)


def _reject_forbidden(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered_key = str(key).lower()
            if lowered_key in FORBIDDEN_KEYS or lowered_key.endswith("_url"):
                raise ValueError(f"Planner output contains forbidden field {path}.{key}.")
            _reject_forbidden(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden(item, f"{path}[{index}]")
    elif isinstance(value, str) and _looks_like_url(value):
        raise ValueError(f"Planner output contains forbidden URL at {path}.")


def _looks_like_url(value: str) -> bool:
    lowered = value.strip().lower()
    return "://" in lowered or lowered.startswith(("localhost:", "127.", "0.0.0.0"))
