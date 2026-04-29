from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.common.models import (
    A2AArtifact,
    A2AMessage,
    A2APart,
    A2ATaskResult,
    A2ATaskState,
)
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient, DirectoryUnavailable
from v2_agent_network.services.pi_agent.agent_runtime import AgentToolCall
from v2_agent_network.services.pi_agent.llm_config import PILLMConfig
from v2_agent_network.services.pi_agent.main import create_app
from v2_agent_network.services.state.store import InMemoryStateStore


EMAIL_CARD = Path("v2_agent_network/configs/agent_cards/email_agent.card.json")
CODING_CARD = Path("v2_agent_network/configs/agent_cards/coding_agent.card.json")


def test_tool_loop_discovers_calls_and_finishes() -> None:
    asyncio.run(_tool_loop_discovers_calls_and_finishes())


async def _tool_loop_discovers_calls_and_finishes() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="discover_agents",
                arguments={"query": "email time", "skill_hint": "email/current_time"},
            ),
            AgentToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/current_time",
                    "task_input": {},
                },
            ),
            AgentToolCall(tool_name="final_answer", arguments={"text": "It is noon."}),
        ]
    )
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "What time is it for the email agent?",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["planner_mode"] == "tool_loop"
    assert payload["llm_model"] == "test-llm"
    assert directory.search_calls == [None, "email/current_time"]
    assert a2a_client.calls == [("email-agent", "email/current_time")]
    assert payload["final_response"] == "It is noon."
    assert payload["task_states"]["email/current_time"] == "completed"
    assert payload["structured_outputs"]["email/current_time"]["skill"] == "email/current_time"
    assert [item["tool_name"] for item in payload["tool_trace"]] == [
        "discover_agents",
        "call_agent",
    ]


def test_tool_loop_initial_state_includes_ads_skill_catalog() -> None:
    asyncio.run(_tool_loop_initial_state_includes_ads_skill_catalog())


async def _tool_loop_initial_state_includes_ads_skill_catalog() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(CODING_CARD))
    llm_client = ScriptedToolLoopClient(
        [AgentToolCall(tool_name="final_answer", arguments={"text": "Catalog inspected."})]
    )
    pi_app = create_app(
        directory_client=directory,
        a2a_client=FailingA2AClient(),
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Can you review this repo?",
            },
        )

    assert response.status_code == 200, response.text
    first_state = llm_client.states[0]
    catalog_skills = {item["skill"] for item in first_state.ads_skill_catalog}
    assert "code/repository_inspection" in catalog_skills
    assert "code/bug_fix" in catalog_skills
    assert first_state.ads_catalog_error is None


def test_tool_loop_cannot_call_undiscovered_agent() -> None:
    asyncio.run(_tool_loop_cannot_call_undiscovered_agent())


async def _tool_loop_cannot_call_undiscovered_agent() -> None:
    a2a_client = FailingA2AClient()
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/current_time",
                    "task_input": {},
                },
            ),
            AgentToolCall(tool_name="final_answer", arguments={"text": "No agent was available."}),
        ]
    )
    pi_app = create_app(
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=a2a_client,
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={"user_id": "user-1", "channel": "api", "text": "What time is it?"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task_ids"] == {}
    assert payload["tool_trace"][0]["status"] == "error"
    assert "has not been discovered" in payload["tool_trace"][0]["content"]["error"]


def test_tool_loop_rejects_raw_endpoint_tool_arguments() -> None:
    asyncio.run(_tool_loop_rejects_raw_endpoint_tool_arguments())


def test_tool_loop_discovery_error_is_observation_not_500() -> None:
    asyncio.run(_tool_loop_discovery_error_is_observation_not_500())


async def _tool_loop_discovery_error_is_observation_not_500() -> None:
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="discover_agents",
                arguments={"query": "code", "skill_hint": "code"},
            ),
            AgentToolCall(
                tool_name="final_answer",
                arguments={"text": "I cannot find a routed coding agent right now."},
            ),
        ]
    )
    pi_app = create_app(
        directory_client=UnavailableDirectoryClient(fake_ads=True),
        a2a_client=FailingA2AClient(),
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Can the coding agent write code?",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["final_response"] == "I cannot find a routed coding agent right now."
    assert payload["tool_trace"][0]["tool_name"] == "discover_agents"
    assert payload["tool_trace"][0]["status"] == "error"
    assert "storage-only diagnostic" in payload["tool_trace"][0]["content"]["error"]


async def _tool_loop_rejects_raw_endpoint_tool_arguments() -> None:
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "http://10.229.117.223:9103/",
                    "skill": "email/current_time",
                    "task_input": {},
                },
            ),
            AgentToolCall(tool_name="final_answer", arguments={"text": "Rejected endpoint override."}),
        ]
    )
    pi_app = create_app(
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailingA2AClient(),
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={"user_id": "user-1", "channel": "api", "text": "Use this endpoint."},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task_ids"] == {}
    assert "not URLs" in payload["tool_trace"][0]["content"]["error"]


def test_tool_loop_write_skill_requires_confirmation_without_a2a() -> None:
    asyncio.run(_tool_loop_write_skill_requires_confirmation_without_a2a())


async def _tool_loop_write_skill_requires_confirmation_without_a2a() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="discover_agents",
                arguments={"query": "send email", "skill_hint": "email/send"},
            ),
            AgentToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {
                        "to": ["wyy0115@outlook.com"],
                        "subject": "Test",
                        "body": "Test",
                    },
                },
            ),
        ]
    )
    state_store = InMemoryStateStore()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=FailingA2AClient(),
        state_store=state_store,
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Send an email to wyy0115@outlook.com.",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["confirmation_required"] is True
    assert payload["pending_confirmation"]["runtime"] == "tool_loop"
    assert payload["pending_confirmation"]["skill"] == "email/send"
    assert payload["task_ids"] == {}
    pending = state_store.get_pending_confirmation(payload["context_id"])
    assert pending["task_input"]["to"] == ["wyy0115@outlook.com"]


def test_tool_loop_confirmation_resumes_pending_call_once() -> None:
    asyncio.run(_tool_loop_confirmation_resumes_pending_call_once())


async def _tool_loop_confirmation_resumes_pending_call_once() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    state_store = InMemoryStateStore()
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="discover_agents",
                arguments={"query": "send email", "skill_hint": "email/send"},
            ),
            AgentToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {
                        "to": ["wyy0115@outlook.com"],
                        "subject": "Test",
                        "body": "Test",
                    },
                },
            ),
        ]
    )
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        state_store=state_store,
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        first = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Send an email to wyy0115@outlook.com.",
            },
        )
        context_id = first.json()["context_id"]
        confirmed = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "context_id": context_id,
                "text": "yes",
            },
        )

    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    assert a2a_client.calls == [("email-agent", "email/send")]
    assert a2a_client.requests[0].metadata["confirmed"] is True
    assert a2a_client.requests[0].metadata["to"] == ["wyy0115@outlook.com"]
    assert payload["confirmation_required"] is False
    assert payload["confirmation_status"] == "confirmed"
    assert payload["structured_output"]["sent_email"]["to"] == "wyy0115@outlook.com"


def test_tool_loop_confirmation_a2a_timeout_returns_failed_response() -> None:
    asyncio.run(_tool_loop_confirmation_a2a_timeout_returns_failed_response())


async def _tool_loop_confirmation_a2a_timeout_returns_failed_response() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = TimeoutA2AClient()
    state_store = InMemoryStateStore()
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="discover_agents",
                arguments={"query": "send email", "skill_hint": "email/send"},
            ),
            AgentToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {
                        "to": ["wyy0115@outlook.com"],
                        "subject": "Test",
                        "body": "Test",
                    },
                },
            ),
        ]
    )
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        state_store=state_store,
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        first = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Send an email to wyy0115@outlook.com.",
            },
        )
        context_id = first.json()["context_id"]
        confirmed = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "context_id": context_id,
                "text": "yes",
            },
        )

    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    assert a2a_client.calls == [("email-agent", "email/send")]
    assert payload["state"] == "failed"
    assert payload["confirmation_required"] is False
    assert payload["confirmation_status"] == "confirmed"
    assert payload["structured_output"]["status"] == "a2a_call_failed"
    assert payload["structured_output"]["error_type"] == "TimeoutError"
    assert "simulated timeout" in payload["final_response"]
    assert state_store.get_pending_confirmation(context_id) is None


def test_tool_loop_a2a_timeout_becomes_tool_observation_not_500() -> None:
    asyncio.run(_tool_loop_a2a_timeout_becomes_tool_observation_not_500())


async def _tool_loop_a2a_timeout_becomes_tool_observation_not_500() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="discover_agents",
                arguments={"query": "email time", "skill_hint": "email/current_time"},
            ),
            AgentToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/current_time",
                    "task_input": {},
                },
            ),
            AgentToolCall(
                tool_name="final_answer",
                arguments={"text": "The email agent timed out."},
            ),
        ]
    )
    pi_app = create_app(
        directory_client=directory,
        a2a_client=TimeoutA2AClient(),
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "What time is it for the email agent?",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["final_response"] == "The email agent timed out."
    assert payload["task_states"]["email/current_time"] == "failed"
    call_observation = payload["tool_trace"][1]
    assert call_observation["tool_name"] == "call_agent"
    assert call_observation["status"] == "failed"
    assert call_observation["content"]["structured_output"]["status"] == "a2a_call_failed"


def test_tool_loop_yes_without_pending_confirmation_continues_agent_loop() -> None:
    asyncio.run(_tool_loop_yes_without_pending_confirmation_continues_agent_loop())


async def _tool_loop_yes_without_pending_confirmation_continues_agent_loop() -> None:
    state_store = InMemoryStateStore()
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="ask_user",
                arguments={"text": "Do you want the simplest Python Hello World program?"},
            ),
            AgentToolCall(
                tool_name="final_answer",
                arguments={"text": 'Here is the simplest Python version:\nprint("Hello, world!")'},
            ),
        ]
    )
    pi_app = create_app(
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailingA2AClient(),
        state_store=state_store,
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        first = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Can you write a hello world program for me?",
            },
        )
        context_id = first.json()["context_id"]
        second = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "context_id": context_id,
                "text": "yes",
            },
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    payload = second.json()
    assert "No pending confirmation" not in second.text
    assert payload["final_response"] == 'Here is the simplest Python version:\nprint("Hello, world!")'
    assert payload["context_id"] == context_id


def test_tool_loop_followup_can_search_read_and_answer_from_observations() -> None:
    asyncio.run(_tool_loop_followup_can_search_read_and_answer_from_observations())


async def _tool_loop_followup_can_search_read_and_answer_from_observations() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="discover_agents",
                arguments={"query": "search email response", "skill_hint": "email/search"},
            ),
            AgentToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/search",
                    "task_input": {"query": "latest response"},
                },
            ),
            AgentToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/read",
                    "task_input": {"message_id": "msg-1"},
                },
            ),
            AgentToolCall(
                tool_name="final_answer",
                arguments={"text": "The latest response says dinner is at 7."},
            ),
        ]
    )
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "What is his response?",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert a2a_client.calls == [
        ("email-agent", "email/search"),
        ("email-agent", "email/read"),
    ]
    assert payload["final_response"] == "The latest response says dinner is at 7."
    assert payload["structured_outputs"]["email/search"]["message_ids"] == ["msg-1"]
    assert payload["structured_outputs"]["email/read"]["body"] == "Dinner is at 7."


def test_tool_loop_dynamic_ads_agent_requires_no_pi_code_change() -> None:
    asyncio.run(_tool_loop_dynamic_ads_agent_requires_no_pi_code_change())


async def _tool_loop_dynamic_ads_agent_requires_no_pi_code_change() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(_logistics_card())
    a2a_client = SpyA2AClient()
    llm_client = ScriptedToolLoopClient(
        [
            AgentToolCall(
                tool_name="discover_agents",
                arguments={"query": "route planning", "skill_hint": "logistics/route_planning"},
            ),
            AgentToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "logistics-agent",
                    "skill": "logistics/route_planning",
                    "task_input": {"destination": "Austin"},
                },
            ),
            AgentToolCall(
                tool_name="final_answer",
                arguments={"text": "The logistics agent returned a route plan."},
            ),
        ]
    )
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        publish_weather_card=False,
        llm_config=_llm_config(),
        tool_loop_llm_client=llm_client,
        agent_mode="tool_loop",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "requested_skill": "logistics/route_planning",
                "text": "Plan a logistics route.",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert directory.search_calls == [None, "logistics/route_planning"]
    assert a2a_client.calls == [("logistics-agent", "logistics/route_planning")]
    assert payload["selected_agent"] == "logistics-agent"
    assert payload["final_response"] == "The logistics agent returned a route plan."


class ScriptedToolLoopClient:
    def __init__(self, calls: list[AgentToolCall]):
        self.calls = calls
        self.states = []

    async def next_tool_call(self, state, tools):
        self.states.append(state.model_copy(deep=True))
        if not self.calls:
            return AgentToolCall(
                tool_name="final_answer",
                arguments={"text": "No more scripted tool calls."},
            )
        return self.calls.pop(0)


class SpyDirectoryClient(DirectoryClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.search_calls: list[str | None] = []

    def search_agents(self, *args, **kwargs):
        skill = kwargs.get("skill")
        if skill is None and args:
            skill = args[0]
        self.search_calls.append(skill)
        return super().search_agents(*args, **kwargs)


class UnavailableDirectoryClient(DirectoryClient):
    def search_agents(self, *args, **kwargs):
        raise DirectoryUnavailable("storage-only diagnostic")


class SpyA2AClient(A2AClient):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.requests = []

    async def send_task(self, agent, request):
        self.calls.append((agent.agent_id, request.skill))
        self.requests.append(request)
        output = _output_for(request)
        return A2ATaskResult(
            task_id=request.task_id or "task-runtime-test",
            context_id=request.context_id,
            state=A2ATaskState.completed,
            messages=[
                A2AMessage(
                    role="agent",
                    parts=[A2APart(kind="text", text=output["summary"])],
                )
            ],
            artifacts=[
                A2AArtifact(
                    artifact_id=request.skill.replace("/", "-"),
                    name=request.skill.replace("/", "_"),
                    parts=[A2APart(kind="data", data=output)],
                )
            ],
            structured_output=output,
        )


class FailingA2AClient(A2AClient):
    async def send_task(self, agent, request):
        raise AssertionError(f"{request.skill} should not have invoked A2A")


class TimeoutA2AClient(A2AClient):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def send_task(self, agent, request):
        self.calls.append((agent.agent_id, request.skill))
        raise TimeoutError("simulated timeout")


def _llm_config() -> PILLMConfig:
    return PILLMConfig(enabled=True, model="test-llm")


def _logistics_card() -> AgentCard:
    return AgentCard(
        name="Logistics Agent",
        description="Test-only route planning agent.",
        url="http://10.0.0.5:9109/",
        provider=AgentProvider(organization="External Logistics", url="https://example.invalid"),
        version="0.1.0",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        defaultInputModes=["text/plain", "application/json"],
        defaultOutputModes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="logistics/route_planning",
                name="Route Planning",
                description="Plan logistics routes.",
                tags=["logistics/route_planning", "logistics", "route", "planning"],
                inputModes=["text/plain", "application/json"],
                outputModes=["text/plain", "application/json"],
            )
        ],
    )


def _output_for(request) -> dict[str, Any]:
    if request.skill == "email/send":
        to = request.metadata.get("to") or []
        if isinstance(to, list):
            to_value = ", ".join(to)
        else:
            to_value = str(to)
        return {
            "skill": "email/send",
            "summary": "Sent.",
            "sent_email": {
                "to": to_value,
                "subject": request.metadata.get("subject"),
            },
        }
    if request.skill == "email/search":
        return {
            "skill": "email/search",
            "summary": "Found one matching response.",
            "message_ids": ["msg-1"],
        }
    if request.skill == "email/read":
        return {
            "skill": "email/read",
            "summary": "Read the latest response.",
            "message_id": "msg-1",
            "body": "Dinner is at 7.",
        }
    if request.skill == "logistics/route_planning":
        return {
            "skill": request.skill,
            "summary": "Route plan ready.",
            "route": ["origin", "Austin"],
        }
    return {
        "skill": request.skill,
        "summary": "It is noon for the email agent.",
        "current_time": "2026-04-29T12:00:00-05:00",
    }
