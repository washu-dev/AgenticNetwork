from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from v2_agent_network.scripts import validate_agent_endpoint
from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.common.models import (
    A2AArtifact,
    A2AMessage,
    A2APart,
    A2ATaskResult,
    A2ATaskState,
)
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app
from v2_agent_network.services.state.store import InMemoryStateStore


EMAIL_CARD = Path("v2_agent_network/configs/agent_cards/email_agent.card.json")


class SpyA2AClient(A2AClient):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.agents = []
        self.requests = []

    async def send_task(self, agent, request):
        self.calls.append((agent.agent_id, request.skill))
        self.agents.append(agent)
        self.requests.append(request)
        output = _output_for(request)
        return A2ATaskResult(
            task_id=request.task_id or "task-real-email-test",
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
                    metadata={"skill": request.skill, "content_type": "application/json"},
                )
            ],
            structured_output=output,
        )


class FailingA2AClient(A2AClient):
    async def send_task(self, agent, request):
        raise AssertionError(f"{request.skill} should not have invoked A2A")


def test_pi_startup_card_paths_loads_live_email_card_into_fake_directory(monkeypatch) -> None:
    monkeypatch.setenv("PI_AGENT_CARD_PATHS", str(EMAIL_CARD))
    directory = DirectoryClient(fake_ads=True)
    app = create_app(directory_client=directory, publish_weather_card=False)

    discovered = app.state.directory_client.search_agents(skill="email/current_time")
    assert discovered[0].endpoint == "http://10.229.117.223:9103/"
    assert discovered[0].organization == "CSPL Email Agent"


def test_requested_skill_current_time_selects_live_email_card_without_placeholder() -> None:
    asyncio.run(_requested_skill_current_time_selects_live_email_card_without_placeholder())


async def _requested_skill_current_time_selects_live_email_card_without_placeholder() -> None:
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    spy_client = SpyA2AClient()
    state_store = InMemoryStateStore()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=spy_client,
        state_store=state_store,
        publish_weather_card=False,
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
                "requested_skill": "email/current_time",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert spy_client.calls == [("email-agent", "email/current_time")]
    assert spy_client.agents[0].endpoint == "http://10.229.117.223:9103/"
    assert spy_client.agents[0].organization == "CSPL Email Agent"
    invocation = state_store.get_invocation(payload["task_ids"]["email/current_time"])
    assert invocation.selected_agent.endpoint == "http://10.229.117.223:9103/"
    assert invocation.selected_agent.organization == "CSPL Email Agent"
    assert payload["confirmation_required"] is False
    _assert_not_placeholder(payload)


def test_requested_skill_email_draft_sets_no_gmail_draft_metadata() -> None:
    asyncio.run(_requested_skill_email_draft_sets_no_gmail_draft_metadata())


async def _requested_skill_email_draft_sets_no_gmail_draft_metadata() -> None:
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    spy_client = SpyA2AClient()
    pi_app = create_app(
        directory_client=directory,
        a2a_client=spy_client,
        publish_weather_card=False,
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
                "text": "Draft a short email to myself. Do not send it.",
                "requested_skill": "email/draft",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert spy_client.calls == [("email-agent", "email/draft")]
    assert spy_client.requests[0].metadata["create_gmail_draft"] is False
    assert payload["task_ids"]["email/draft"]
    assert payload["structured_output"]["gmail_draft_created"] is False
    _assert_not_placeholder(payload)


def test_requested_skill_email_send_confirmation_required_without_invocation() -> None:
    asyncio.run(_requested_skill_email_send_confirmation_required_without_invocation())


async def _requested_skill_email_send_confirmation_required_without_invocation() -> None:
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    pi_app = create_app(
        directory_client=directory,
        a2a_client=FailingA2AClient(),
        publish_weather_card=False,
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
                "text": "Send this email.",
                "requested_skill": "email/send",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["confirmation_required"] is True
    assert payload["pending_confirmation"]["skill"] == "email/send"
    assert payload["task_ids"] == {}


def test_email_draft_create_gmail_draft_true_confirmation_required_without_invocation() -> None:
    asyncio.run(_email_draft_create_gmail_draft_true_confirmation_required_without_invocation())


async def _email_draft_create_gmail_draft_true_confirmation_required_without_invocation() -> None:
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    pi_app = create_app(
        directory_client=directory,
        a2a_client=FailingA2AClient(),
        publish_weather_card=False,
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
                "text": "Draft this email and create a Gmail draft.",
                "requested_skill": "email/draft",
                "metadata": {"create_gmail_draft": True},
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["confirmation_required"] is True
    assert payload["pending_confirmation"]["skill"] == "email/draft"
    assert payload["task_ids"] == {}


def test_validate_endpoint_email_draft_uses_safe_metadata() -> None:
    report = asyncio.run(
        validate_agent_endpoint.validate_endpoint(
            card_path=EMAIL_CARD,
            skill="email/draft",
            prompt="Draft a short test email to myself, but do not create a Gmail draft and do not send it.",
            endpoint="http://127.0.0.1:9/",
            timeout=0.01,
        )
    )

    assert report["request_metadata"]["create_gmail_draft"] is False
    assert report["a2a_task_sent"] is False


@pytest.mark.skipif(
    not os.getenv("LIVE_EMAIL_AGENT_URL"),
    reason="Set LIVE_EMAIL_AGENT_URL to run live Email Agent endpoint validation.",
)
def test_live_email_agent_current_time_endpoint_manual_integration() -> None:
    report = asyncio.run(
        validate_agent_endpoint.validate_endpoint(
            card_path=EMAIL_CARD,
            skill="email/current_time",
            prompt="What time is it for the email agent?",
            endpoint=os.environ["LIVE_EMAIL_AGENT_URL"],
        )
    )

    assert report["errors"] == []
    assert report["a2a_task_state"] == "completed"


@pytest.mark.skipif(
    not os.getenv("LIVE_EMAIL_AGENT_URL"),
    reason="Set LIVE_EMAIL_AGENT_URL to run live PI -> Email Agent integration.",
)
def test_pi_current_time_reaches_live_email_endpoint_manual_integration() -> None:
    asyncio.run(_pi_current_time_reaches_live_email_endpoint_manual_integration())


async def _pi_current_time_reaches_live_email_endpoint_manual_integration() -> None:
    card = load_agent_card(EMAIL_CARD).model_copy(update={"url": os.environ["LIVE_EMAIL_AGENT_URL"]})
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(card)
    pi_app = create_app(
        directory_client=directory,
        publish_weather_card=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "integration-user",
                "channel": "api",
                "text": "What time is it for the email agent?",
                "requested_skill": "email/current_time",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task_ids"]["email/current_time"]
    assert payload["confirmation_required"] is False


def _output_for(request) -> dict:
    if request.skill == "email/current_time":
        return {
            "skill": "email/current_time",
            "summary": "The email agent test clock is available.",
            "current_time": "2026-04-29T12:00:00-05:00",
            "timezone": "America/Chicago",
        }
    if request.skill == "email/draft":
        return {
            "skill": "email/draft",
            "summary": "Prepared an email draft for review.",
            "draft_email": {
                "to": "me@example.com",
                "subject": "AgenticNetwork A2A integration test",
                "body": "This is an AgenticNetwork A2A integration test.",
            },
            "gmail_draft_created": False,
        }
    return {"skill": request.skill, "summary": f"Completed {request.skill}."}


def _assert_not_placeholder(payload: dict) -> None:
    serialized = json.dumps(payload, sort_keys=True)
    assert "Placeholder executor only" not in serialized
    assert "placeholder" not in serialized
    assert "no Gmail draft was created" not in serialized
