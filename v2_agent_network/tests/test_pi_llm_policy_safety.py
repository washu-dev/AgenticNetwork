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
    AgentCapability,
    DiscoveredAgent,
    PublishedRecord,
)
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.llm_config import PILLMConfig
from v2_agent_network.services.pi_agent.llm_planner import PIPlannerPlan, PIPlannerStep
from v2_agent_network.services.pi_agent.main import create_app
from v2_agent_network.services.pi_agent.skill_catalog import build_skill_catalog
from v2_agent_network.services.state.store import InMemoryStateStore


EMAIL_CARD = Path("v2_agent_network/configs/agent_cards/email_agent.card.json")


def test_llm_planned_current_time_uses_directory_policy_and_a2a() -> None:
    asyncio.run(_llm_planned_current_time_uses_directory_policy_and_a2a())


async def _llm_planned_current_time_uses_directory_policy_and_a2a() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    state_store = InMemoryStateStore()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        state_store=state_store,
        publish_weather_card=False,
        llm_config=_llm_config(),
        llm_planner=StaticPlanner("email/current_time"),
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
    assert payload["planner_mode"] == "llm"
    assert payload["llm_model"] == "test-llm"
    assert "email/current_time" in directory.search_calls
    assert a2a_client.calls == [("email-agent", "email/current_time")]
    assert payload["policy_decision"] == "allowed"
    assert payload["confirmation_required"] is False
    assert payload["task_states"]["email/current_time"] == "completed"
    assert payload["structured_outputs"]["email/current_time"]["skill"] == "email/current_time"
    invocation = state_store.get_invocation(payload["task_ids"]["email/current_time"])
    assert invocation.selected_agent.endpoint == "http://10.229.117.223:9103/"
    assert invocation.policy_decision.status == "allowed"


def test_llm_planned_email_send_creates_draft_then_requires_confirmation() -> None:
    asyncio.run(_llm_planned_email_send_creates_draft_then_requires_confirmation())


async def _llm_planned_email_send_creates_draft_then_requires_confirmation() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        publish_weather_card=False,
        llm_config=_llm_config(),
        llm_planner=StaticPlanner(
            "email/send",
            requires_confirmation=True,
            required_inputs={
                "to": ["bob@example.com"],
                "subject": "Hello",
                "body": "Hello Bob.",
                "create_gmail_draft": False,
            },
        ),
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
                "text": "Send an email to Bob saying hello.",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["planner_mode"] == "llm"
    assert "email/send" in directory.search_calls
    assert payload["workflow"] == ["email/draft", "email/send"]
    assert a2a_client.calls == [("email-agent", "email/draft")]
    assert payload["draft_email"]["to"] == "bob@example.com"
    assert payload["confirmation_required"] is True
    assert payload["pending_confirmation"]["skill"] == "email/send"
    assert payload["task_ids"]["email/draft"]


def test_llm_email_draft_required_inputs_are_forwarded_to_a2a() -> None:
    asyncio.run(_llm_email_draft_required_inputs_are_forwarded_to_a2a())


async def _llm_email_draft_required_inputs_are_forwarded_to_a2a() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        publish_weather_card=False,
        llm_config=_llm_config(),
        llm_planner=StaticPlanner(
            "email/draft",
            required_inputs={
                "to": ["wyy0115@outlook.com"],
                "subject": "Dinner",
                "body": "What is for dinner?",
                "create_gmail_draft": False,
            },
        ),
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
                "text": "Can you send an email to wyy0115@outlook.com and ask what is for dinner?",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert a2a_client.calls == [("email-agent", "email/draft")]
    request = a2a_client.requests[0]
    assert request.metadata["to"] == ["wyy0115@outlook.com"]
    assert request.metadata["subject"] == "Dinner"
    assert request.metadata["body"] == "What is for dinner?"
    assert request.metadata["create_gmail_draft"] is False
    assert payload["draft_email"]["to"] == "wyy0115@outlook.com"
    assert payload["draft_email"]["subject"] == "Dinner"
    assert "dinner" in payload["draft_email"]["body"].lower()
    assert payload["confirmation_required"] is True
    assert payload["pending_confirmation"]["skill"] == "email/send"


def test_confirmation_endpoint_sends_prepared_email_after_explicit_confirmation() -> None:
    asyncio.run(_confirmation_endpoint_sends_prepared_email_after_explicit_confirmation())


async def _confirmation_endpoint_sends_prepared_email_after_explicit_confirmation() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    state_store = InMemoryStateStore()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        state_store=state_store,
        publish_weather_card=False,
        llm_config=_llm_config(),
        llm_planner=StaticPlanner(
            "email/draft",
            required_inputs={
                "to": ["wyy0115@outlook.com"],
                "subject": "Dinner",
                "body": "What is for dinner?",
                "create_gmail_draft": False,
            },
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        draft_response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Can you send an email to wyy0115@outlook.com and ask what is for dinner?",
            },
        )
        assert draft_response.status_code == 200, draft_response.text
        context_id = draft_response.json()["context_id"]
        assert a2a_client.calls == [("email-agent", "email/draft")]

        send_response = await pi_http_client.post(
            "/confirmations",
            json={
                "user_id": "user-1",
                "channel": "api",
                "context_id": context_id,
                "skill": "email/send",
                "confirmed": True,
            },
        )

    assert send_response.status_code == 200, send_response.text
    payload = send_response.json()
    assert a2a_client.calls == [
        ("email-agent", "email/draft"),
        ("email-agent", "email/send"),
    ]
    send_request = a2a_client.requests[1]
    assert send_request.metadata["confirmed"] is True
    assert send_request.metadata["confirmation_status"] == "confirmed"
    assert send_request.metadata["previous_outputs"]["email/draft"]["draft_email"]["to"] == (
        "wyy0115@outlook.com"
    )
    assert payload["policy_decision"] == "allowed"
    assert payload["confirmation_status"] == "confirmed"
    assert payload["confirmation_required"] is False
    assert payload["structured_output"]["sent_email"]["to"] == "wyy0115@outlook.com"


def test_chat_yes_confirms_prepared_email_send() -> None:
    asyncio.run(_chat_yes_confirms_prepared_email_send())


async def _chat_yes_confirms_prepared_email_send() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    state_store = InMemoryStateStore()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        state_store=state_store,
        publish_weather_card=False,
        llm_config=_llm_config(),
        llm_planner=StaticPlanner(
            "email/draft",
            required_inputs={
                "to": ["wyy0115@outlook.com"],
                "subject": "Dinner",
                "body": "What is for dinner?",
                "create_gmail_draft": False,
            },
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        draft_response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "Can you send an email to wyy0115@outlook.com and ask what is for dinner?",
            },
        )
        context_id = draft_response.json()["context_id"]
        send_response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "context_id": context_id,
                "text": "yes",
            },
        )

    assert send_response.status_code == 200, send_response.text
    payload = send_response.json()
    assert a2a_client.calls == [
        ("email-agent", "email/draft"),
        ("email-agent", "email/send"),
    ]
    assert payload["confirmation_status"] == "confirmed"
    assert payload["confirmation_required"] is False
    assert payload["structured_output"]["sent_email"]["to"] == "wyy0115@outlook.com"


def test_followup_message_reuses_context_history_and_outputs() -> None:
    asyncio.run(_followup_message_reuses_context_history_and_outputs())


async def _followup_message_reuses_context_history_and_outputs() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    state_store = InMemoryStateStore()
    planner = StaticPlanner("email/current_time")
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        state_store=state_store,
        publish_weather_card=False,
        llm_config=_llm_config(),
        llm_planner=planner,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        first_response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "text": "What time is it for the email agent?",
            },
        )
        context_id = first_response.json()["context_id"]
        second_response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "user-1",
                "channel": "api",
                "context_id": context_id,
                "text": "What was the previous result?",
            },
        )

    assert second_response.status_code == 200, second_response.text
    context = state_store.get_context(context_id)
    assert context.conversation_history == [
        "What time is it for the email agent?",
        "What was the previous result?",
    ]
    second_context = planner.conversation_contexts[-1]
    assert second_context["conversation_history"] == context.conversation_history
    assert second_context["recent_invocations"][0]["skill"] == "email/current_time"
    assert second_context["previous_outputs"]["email/current_time"]["current_time"] == (
        "2026-04-29T12:00:00-05:00"
    )


def test_llm_synthesizes_final_response_from_tool_outputs() -> None:
    asyncio.run(_llm_synthesizes_final_response_from_tool_outputs())


async def _llm_synthesizes_final_response_from_tool_outputs() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    planner = StaticPlanner(
        ["email/search", "email/read"],
        synthesis_text="The latest relevant response says he can share the plan soon.",
    )
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        publish_weather_card=False,
        llm_config=_llm_config(),
        llm_planner=planner,
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
    assert payload["final_response"] == (
        "The latest relevant response says he can share the plan soon."
    )
    assert payload["text"] == payload["final_response"]
    assert "email/search" in planner.synthesis_payloads[-1]["structured_outputs"]
    assert "email/read" in planner.synthesis_payloads[-1]["structured_outputs"]


def test_agent_card_write_skill_tag_requires_confirmation_without_a2a() -> None:
    asyncio.run(_agent_card_write_skill_tag_requires_confirmation_without_a2a())


async def _agent_card_write_skill_tag_requires_confirmation_without_a2a() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(_group_email_card())
    pi_app = create_app(
        directory_client=directory,
        a2a_client=FailingA2AClient(),
        publish_weather_card=False,
        llm_config=_llm_config(),
        llm_planner=StaticPlanner(
            "email/group_send",
            required_inputs={
                "subject": "test",
                "body": "test",
                "confirmed": False,
            },
        ),
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
                "text": "Can you send a group notice email with title test and content test?",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "email/group_send" in directory.search_calls
    assert payload["workflow"] == ["email/group_send"]
    assert payload["confirmation_required"] is True
    assert payload["pending_confirmation"]["skill"] == "email/group_send"
    assert payload["policy_decision"] == "requires_confirmation"
    assert payload["task_ids"] == {}


def test_pi_chat_ui_is_served() -> None:
    asyncio.run(_pi_chat_ui_is_served())


async def _pi_chat_ui_is_served() -> None:
    pi_app = create_app(
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailingA2AClient(),
        publish_weather_card=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.get("/")

    assert response.status_code == 200
    assert "PI Agent Chat" in response.text
    assert 'fetch("/messages"' in response.text


def test_confirmation_false_does_not_invoke_email_send() -> None:
    asyncio.run(_confirmation_false_does_not_invoke_email_send())


async def _confirmation_false_does_not_invoke_email_send() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a_client = SpyA2AClient()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=a2a_client,
        publish_weather_card=False,
        llm_config=_llm_config(),
        llm_planner=StaticPlanner("email/draft", required_inputs={"to": ["wyy0115@outlook.com"]}),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        draft_response = await pi_http_client.post(
            "/messages",
            json={"user_id": "user-1", "channel": "api", "text": "Draft a test email."},
        )
        context_id = draft_response.json()["context_id"]
        response = await pi_http_client.post(
            "/confirmations",
            json={
                "user_id": "user-1",
                "channel": "api",
                "context_id": context_id,
                "skill": "email/send",
                "confirmed": False,
            },
        )

    assert response.status_code == 200, response.text
    assert a2a_client.calls == [("email-agent", "email/draft")]
    assert response.json()["confirmation_status"] == "rejected"


def test_llm_cannot_select_agent_that_directory_did_not_discover() -> None:
    asyncio.run(_llm_cannot_select_agent_that_directory_did_not_discover())


async def _llm_cannot_select_agent_that_directory_did_not_discover() -> None:
    directory = SpyDirectoryClient(fake_ads=True)
    pi_app = create_app(
        directory_client=directory,
        a2a_client=FailingA2AClient(),
        publish_weather_card=False,
        llm_config=_llm_config(),
        llm_planner=StaticPlanner("email/current_time"),
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
    assert payload["text"].startswith("No capable agent") or payload["workflow"] == []
    assert payload["selected_agents"] == []
    assert payload["task_ids"] == {}


def test_real_ads_mode_ignores_pi_agent_card_paths(monkeypatch) -> None:
    monkeypatch.setenv("PI_AGENT_CARD_PATHS", str(EMAIL_CARD))
    directory = DirectoryClient(fake_ads=False, backend=EmptyBackend())
    app = create_app(directory_client=directory, publish_weather_card=False)

    assert app.state.directory_client.search_agents(skill="email/current_time") == []


def test_skill_catalog_includes_discovered_custom_skill_without_static_list() -> None:
    catalog = build_skill_catalog(DynamicAgentDirectory())

    assert any(item.skill == "logistics/load_balancing" for item in catalog)


class StaticPlanner:
    def __init__(
        self,
        skill: str | list[str],
        *,
        requires_confirmation: bool = False,
        required_inputs: dict[str, Any] | None = None,
        synthesis_text: str | None = None,
    ):
        self.skills = [skill] if isinstance(skill, str) else list(skill)
        self.requires_confirmation = requires_confirmation
        self.required_inputs = required_inputs or {}
        self.synthesis_text = synthesis_text
        self.available_skills = []
        self.conversation_contexts = []
        self.synthesis_payloads = []

    async def plan(self, **kwargs):
        self.available_skills = kwargs["available_skills"]
        self.conversation_contexts.append(kwargs["conversation_context"])
        available = {item.skill for item in self.available_skills}
        missing = [skill for skill in self.skills if skill not in available]
        if missing:
            return PIPlannerPlan(
                workflow=[],
                needs_clarification=True,
                clarifying_question=f"No capable agent found for {missing[0]}.",
                user_visible_plan="No capable agent was discovered.",
            )
        return PIPlannerPlan(
            workflow=[
                PIPlannerStep(
                    skill=skill,
                    reason="Mocked LLM plan.",
                    confidence=0.95,
                    risk_hint="write" if self.requires_confirmation else "read",
                    requires_confirmation=self.requires_confirmation,
                    required_inputs=self.required_inputs,
                )
                for skill in self.skills
            ],
            needs_clarification=False,
            clarifying_question=None,
            user_visible_plan=f"I will use {', '.join(self.skills)}.",
        )

    async def synthesize_response(self, **kwargs):
        self.synthesis_payloads.append(kwargs)
        return self.synthesis_text or kwargs["fallback_text"]


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


class SpyA2AClient(A2AClient):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.requests = []

    async def send_task(self, agent, request):
        self.calls.append((agent.agent_id, request.skill))
        self.requests.append(request)
        output = _output_for(request)
        return A2ATaskResult(
            task_id=request.task_id or "task-llm-test",
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


class EmptyBackend:
    def publish_agent_card(self, card: AgentCard) -> PublishedRecord:
        raise AssertionError("Real ADS mode should not publish PI_AGENT_CARD_PATHS.")

    def publish_oasf_record(self, record: dict[str, Any]) -> PublishedRecord:
        raise AssertionError("Real ADS mode should not publish PI_AGENT_CARD_PATHS.")

    def search_agents(self, **kwargs) -> list[DiscoveredAgent]:
        return []

    def resolve_agent(self, agent_id_or_record_id: str) -> DiscoveredAgent:
        raise KeyError(agent_id_or_record_id)

    def healthcheck(self) -> bool:
        return True


class DynamicAgentDirectory:
    fake_ads = False

    def search_agents(self, *args, **kwargs):
        skill = kwargs.get("skill")
        capability = AgentCapability(
            name="Load balancing",
            description="Plan freight load balancing.",
            skill_tags=["logistics/load_balancing"],
            custom_skill_tags=["logistics/load_balancing"],
            input_modes=["text/plain"],
            output_modes=["application/json"],
        )
        agent = DiscoveredAgent(
            agent_id="dynamic-logistics-agent",
            name="Dynamic Logistics Agent",
            organization="External",
            description="Dynamically discovered logistics agent.",
            skills=[capability],
            domains=["logistics"],
            input_modes=["text/plain"],
            output_modes=["application/json"],
            endpoint="http://10.0.0.5:9109/",
            source_record_id="dynamic-logistics-record",
        )
        if skill and skill != "logistics/load_balancing":
            return []
        return [agent]


def _group_email_card() -> AgentCard:
    return AgentCard(
        name="Email Agent",
        description="Email agent with group send support.",
        url="http://10.229.117.223:9103/",
        provider=AgentProvider(organization="CSPL Email Agent", url="https://example.invalid"),
        version="0.2.1",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        defaultInputModes=["text/plain", "application/json"],
        defaultOutputModes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="email/search",
                name="Search Email",
                description="Search Gmail message metadata.",
                tags=["email/search", "email", "gmail", "search"],
                inputModes=["text/plain", "application/json"],
                outputModes=["text/plain", "application/json"],
            ),
            AgentSkill(
                id="email/group_send",
                name="Send Group Email",
                description="Send one Gmail message to a group after explicit confirmation.",
                tags=[
                    "email/group_send",
                    "email/send",
                    "email",
                    "gmail",
                    "send",
                    "group",
                    "write",
                ],
                inputModes=["text/plain", "application/json"],
                outputModes=["text/plain", "application/json"],
            ),
        ],
    )


def _llm_config() -> PILLMConfig:
    return PILLMConfig(enabled=True, model="test-llm")


def _output_for(request) -> dict:
    if request.skill == "email/draft":
        to = request.metadata.get("to") or []
        if isinstance(to, list):
            to_value = ", ".join(to)
        else:
            to_value = str(to)
        draft = {
            "to": to_value,
            "subject": str(request.metadata.get("subject") or "Email draft"),
            "body": str(request.metadata.get("body") or ""),
        }
        return {
            "skill": "email/draft",
            "summary": "Prepared an email draft for review.",
            "draft_email": draft,
            "gmail_draft_created": bool(request.metadata.get("create_gmail_draft")),
        }
    if request.skill == "email/send":
        previous_outputs = request.metadata.get("previous_outputs") or {}
        draft = previous_outputs.get("email/draft", {}).get("draft_email", {})
        return {
            "skill": "email/send",
            "summary": "Sent the email.",
            "status": "completed",
            "sent_email": {
                "to": draft.get("to"),
                "subject": draft.get("subject"),
            },
        }
    return {
        "skill": request.skill,
        "summary": "It is noon for the email agent.",
        "current_time": "2026-04-29T12:00:00-05:00",
        "timezone": "America/Chicago",
    }
