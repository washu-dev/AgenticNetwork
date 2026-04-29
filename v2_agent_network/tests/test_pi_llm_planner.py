from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from v2_agent_network.services.pi_agent.llm_config import PILLMConfig
from v2_agent_network.services.pi_agent.llm_planner import (
    PILLMPlanner,
    PILLMPlannerError,
    PIPlannerPlan,
    _openai_strict_schema,
)
from v2_agent_network.services.pi_agent.skill_catalog import AvailableSkill


def test_llm_planner_parses_strict_json_plan() -> None:
    asyncio.run(_llm_planner_parses_strict_json_plan())


async def _llm_planner_parses_strict_json_plan() -> None:
    fake_client = FakeOpenAIClient(_plan_output("email/current_time"))
    planner = PILLMPlanner(_config(), openai_client=fake_client)

    plan = await planner.plan(
        user_text="What time is it for the email agent?",
        requested_skill=None,
        available_skills=[_available_skill("email/current_time")],
    )

    assert [step.skill for step in plan.workflow] == ["email/current_time"]
    assert plan.needs_clarification is False
    assert fake_client.responses.calls[0]["model"] == "test-model"
    assert fake_client.responses.calls[0]["text"]["format"]["type"] == "json_schema"
    schema = fake_client.responses.calls[0]["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert "$defs" not in schema
    step_schema = schema["properties"]["workflow"]["items"]
    required_inputs_schema = step_schema["properties"]["required_inputs"]
    assert required_inputs_schema["additionalProperties"] is False


def test_openai_strict_schema_is_inline_and_closes_objects() -> None:
    schema = _openai_strict_schema(PIPlannerPlan.model_json_schema())

    assert schema["additionalProperties"] is False
    assert "workflow" in schema["required"]
    assert "$defs" not in schema
    assert "default" not in json.dumps(schema)
    step_schema = schema["properties"]["workflow"]["items"]
    assert step_schema["additionalProperties"] is False
    assert step_schema["properties"]["required_inputs"]["additionalProperties"] is False
    assert set(step_schema["properties"]["required_inputs"]["required"]) == {
        "to",
        "cc",
        "bcc",
        "recipient_name",
        "subject",
        "body",
        "create_gmail_draft",
        "confirmed",
    }
    assert step_schema["properties"]["required_inputs"]["properties"]["to"]["anyOf"][0]["type"] == "array"


def test_llm_planner_rejects_endpoint_fields() -> None:
    asyncio.run(_llm_planner_rejects_endpoint_fields())


async def _llm_planner_rejects_endpoint_fields() -> None:
    output = {
        "workflow": [
            {
                "skill": "email/current_time",
                "reason": "Use the endpoint directly.",
                "confidence": 0.95,
                "risk_hint": "read",
                "requires_confirmation": False,
                "required_inputs": {},
                "endpoint": "http://127.0.0.1:9103/",
            }
        ],
        "needs_clarification": False,
        "clarifying_question": None,
        "user_visible_plan": "Use current time.",
    }
    planner = PILLMPlanner(_config(), openai_client=FakeOpenAIClient(json.dumps(output)))

    with pytest.raises(PILLMPlannerError) as exc_info:
        await planner.plan(
            user_text="What time is it?",
            requested_skill=None,
            available_skills=[_available_skill("email/current_time")],
        )

    assert "forbidden field" in str(exc_info.value)


def test_llm_planner_rejects_raw_urls_in_output() -> None:
    asyncio.run(_llm_planner_rejects_raw_urls_in_output())


async def _llm_planner_rejects_raw_urls_in_output() -> None:
    planner = PILLMPlanner(
        _config(),
        openai_client=FakeOpenAIClient(
            _plan_output("email/current_time", reason="Call http://127.0.0.1:9103/ now.")
        ),
    )

    with pytest.raises(PILLMPlannerError) as exc_info:
        await planner.plan(
            user_text="What time is it?",
            requested_skill=None,
            available_skills=[_available_skill("email/current_time")],
        )

    assert "forbidden URL" in str(exc_info.value)


def test_llm_planner_rejects_unavailable_skill_selection() -> None:
    asyncio.run(_llm_planner_rejects_unavailable_skill_selection())


async def _llm_planner_rejects_unavailable_skill_selection() -> None:
    planner = PILLMPlanner(_config(), openai_client=FakeOpenAIClient(_plan_output("email/send")))

    with pytest.raises(PILLMPlannerError) as exc_info:
        await planner.plan(
            user_text="Send an email to Bob.",
            requested_skill=None,
            available_skills=[_available_skill("email/current_time")],
        )

    assert "unavailable skill" in str(exc_info.value)


def test_requested_skill_must_be_in_available_skills() -> None:
    asyncio.run(_requested_skill_must_be_in_available_skills())


async def _requested_skill_must_be_in_available_skills() -> None:
    fake_client = FakeOpenAIClient(_plan_output("email/send"))
    planner = PILLMPlanner(_config(), openai_client=fake_client)

    plan = await planner.plan(
        user_text="Send an email to Bob.",
        requested_skill="email/send",
        available_skills=[_available_skill("email/current_time")],
    )

    assert plan.workflow == []
    assert plan.needs_clarification is True
    assert fake_client.responses.calls == []


class FakeResponses:
    def __init__(self, output_text: str):
        self.output_text = output_text
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


class FakeOpenAIClient:
    def __init__(self, output_text: str):
        self.responses = FakeResponses(output_text)


def _config() -> PILLMConfig:
    return PILLMConfig(enabled=True, model="test-model")


def _available_skill(skill: str) -> AvailableSkill:
    return AvailableSkill(
        skill=skill,
        agent_id="email-agent",
        agent_name="Email Agent",
        description="Email skill",
        risk_level="low",
        action_type="read",
        input_modes=["text/plain"],
        output_modes=["application/json"],
        examples=[],
    )


def _plan_output(skill: str, *, reason: str | None = None) -> str:
    return json.dumps(
        {
            "workflow": [
                {
                    "skill": skill,
                    "reason": reason or f"The user needs {skill}.",
                    "confidence": 0.95,
                    "risk_hint": "read",
                    "requires_confirmation": False,
                    "required_inputs": {},
                }
            ],
            "needs_clarification": False,
            "clarifying_question": None,
            "user_visible_plan": f"I will call {skill}.",
        }
    )
