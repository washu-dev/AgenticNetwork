from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx
from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

from v3_main_agent.main_agent.a2a.client import A2AClient
from v3_main_agent.main_agent.models import (
    A2AArtifact,
    A2AMessage,
    A2APart,
    A2ATaskResult,
    A2ATaskState,
    Channel,
)
from v3_main_agent.main_agent.directory.card_oasf_converter import load_agent_card
from v3_main_agent.main_agent.directory.client import DirectoryClient
from v3_main_agent.main_agent.state.store import InMemoryStateStore

from v3_main_agent.main_agent.config import MainAgentConfig
from v3_main_agent.main_agent.llm_client import ScriptedLLMClient, ToolCall
from v3_main_agent.main_agent.server import create_app


REPO_ROOT = Path(__file__).resolve().parents[2]
EMAIL_CARD = REPO_ROOT / "v3_main_agent" / "configs" / "agent_cards" / "email_agent.card.json"
WEATHER_CARD = REPO_ROOT / "v3_main_agent" / "configs" / "agent_cards" / "weather_agent.card.json"
CODING_CARD = REPO_ROOT / "v3_main_agent" / "configs" / "agent_cards" / "coding_agent.card.json"


# ---------- mock support ----------


class SpyDirectory(DirectoryClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.search_calls: list[str | None] = []

    def search_agents(self, *args, **kwargs):
        skill = kwargs.get("skill")
        if skill is None and args:
            skill = args[0]
        self.search_calls.append(skill)
        return super().search_agents(*args, **kwargs)


class SpyA2A(A2AClient):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.requests: list = []

    async def send_task(self, agent, request):
        self.calls.append((agent.agent_id, request.skill))
        self.requests.append(request)
        out = _mock_output(request)
        artifact_key = (request.skill or out.get("chosen_skill") or out.get("skill") or "agent/autonomous")
        return A2ATaskResult(
            task_id=request.task_id or "task-mock",
            context_id=request.context_id,
            state=A2ATaskState.completed,
            messages=[
                A2AMessage(role="agent", parts=[A2APart(kind="text", text=out["summary"])])
            ],
            artifacts=[
                A2AArtifact(
                    artifact_id=artifact_key.replace("/", "-"),
                    name=artifact_key.replace("/", "_"),
                    parts=[A2APart(kind="data", data=out)],
                )
            ],
            structured_output=out,
        )


class ProgressA2A(SpyA2A):
    async def send_task(self, agent, request, progress_callback=None):
        if progress_callback is not None:
            await progress_callback(
                {
                    "tool": "remote_status",
                    "status": "working",
                    "content": {
                        "agent_id": agent.agent_id,
                        "state": "working",
                        "event": "scene_started",
                        "text": "Scene assessment started with Gemini.",
                    },
                }
            )
        return await super().send_task(agent, request)


class FailA2A(A2AClient):
    async def send_task(self, agent, request):
        raise AssertionError(f"A2A should not be called for {request.skill}")


def _mock_output(request) -> dict[str, Any]:
    skill = request.skill or "agent/autonomous"
    if skill == "weather/forecast":
        return {
            "skill": "weather/forecast",
            "requested_skill": request.skill,
            "chosen_skill": "weather/forecast",
            "summary": "Mock forecast for Austin: 78°F, sunny.",
            "location": "Austin",
            "temperature_f": 78,
        }
    if skill == "email/current_time":
        return {
            "skill": "email/current_time",
            "requested_skill": request.skill,
            "chosen_skill": "email/current_time",
            "summary": "It's 2:15 PM.",
            "current_time": "14:15",
        }
    if skill == "email/send":
        return {
            "skill": "email/send",
            "requested_skill": request.skill,
            "chosen_skill": "email/send",
            "summary": "Email sent.",
            "sent_email": {
                "to": ", ".join(request.metadata.get("to") or []) or request.metadata.get("to"),
                "subject": request.metadata.get("subject"),
            },
        }
    if skill == "email/draft":
        return {
            "skill": "email/draft",
            "requested_skill": request.skill,
            "chosen_skill": "email/draft",
            "summary": "Drafted email.",
            "draft_email": {
                "to": ", ".join(request.metadata.get("to") or []),
                "subject": request.metadata.get("subject", "(no subject)"),
                "body": request.metadata.get("body", ""),
            },
        }
    return {
        "skill": "agent/autonomous",
        "requested_skill": request.skill,
        "chosen_skill": "agent/autonomous",
        "summary": f"{skill} done.",
    }


def _config(policy: str = "permissive") -> MainAgentConfig:
    return MainAgentConfig(
        llm_enabled=False,
        llm_model="mock-llm",
        max_steps=8,
        fake_ads=True,
        policy_mode=policy,
    )


# ---------- tests ----------


def test_list_agents_returns_full_directory() -> None:
    asyncio.run(_list_agents_returns_full_directory())


def test_progress_callback_receives_tool_trace_entries() -> None:
    asyncio.run(_progress_callback_receives_tool_trace_entries())


async def _list_agents_returns_full_directory() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    directory.publish_agent_card(load_agent_card(WEATHER_CARD))
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="final_answer",
                arguments={"text": "We have an email agent and a weather agent."},
            ),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=FailA2A(),
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "What agents are available now?"},
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["state"] == "completed"
    assert "email agent" in payload["final_response"].lower()
    list_obs = next(t for t in payload["tool_trace"] if t["tool"] == "list_agents")
    agent_ids = {a["agent_id"] for a in list_obs["content"]["agents"]}
    assert {"email-agent", "weather-agent"}.issubset(agent_ids)


async def _progress_callback_receives_tool_trace_entries() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    llm = ScriptedLLMClient(
        [
            ToolCall(
                tool_name="list_agents",
                arguments={},
                reasoning="Need to discover who can help.",
            ),
            ToolCall(
                tool_name="final_answer",
                arguments={"text": "Done."},
                reasoning="Now answer with the discovered context.",
            ),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=FailA2A(),
        llm_client=llm,
    )
    streamed: list[dict[str, Any]] = []

    async def progress(entry: dict[str, Any]) -> None:
        streamed.append(entry)

    result = await app.state.runtime.handle_message(
        user_id="u",
        channel=Channel.api,
        text="What agents are available?",
        progress_callback=progress,
    )

    assert result.state == A2ATaskState.completed
    assert [entry["tool"] for entry in streamed] == [
        entry["tool"] for entry in result.tool_trace
    ] == ["reasoning", "list_agents", "reasoning", "final_answer"]


def test_describe_agent_returns_full_skill_catalog() -> None:
    asyncio.run(_describe_agent_returns_full_skill_catalog())


async def _describe_agent_returns_full_skill_catalog() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(tool_name="describe_agent", arguments={"agent_id": "email-agent"}),
            ToolCall(
                tool_name="final_answer",
                arguments={"text": "The email agent supports search, read, draft, send, and more."},
            ),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=FailA2A(),
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "What can the email agent do?"},
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    describe_obs = next(t for t in payload["tool_trace"] if t["tool"] == "describe_agent")
    skill_ids = {sid for s in describe_obs["content"]["skills"] for sid in s["skill_ids"]}
    assert "email/send" in skill_ids
    assert "email/draft" in skill_ids


def test_call_agent_invokes_a2a_and_returns_structured_output() -> None:
    asyncio.run(_call_agent_invokes_a2a())


async def _call_agent_invokes_a2a() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(WEATHER_CARD))
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "weather-agent",
                    "skill": "weather/forecast",
                    "task_input": {"location": "Austin"},
                },
            ),
            ToolCall(
                tool_name="final_answer",
                arguments={"text": "Austin forecast: 78°F, sunny."},
            ),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "What's the weather in Austin?"},
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert a2a.calls == [("weather-agent", "weather/forecast")]
    assert payload["structured_outputs"]["weather/forecast"]["location"] == "Austin"
    assert payload["selected_agent"] == "weather-agent"


def test_call_agent_without_skill_invokes_a2a() -> None:
    asyncio.run(_call_agent_without_skill())


async def _call_agent_without_skill() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(WEATHER_CARD))
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "weather-agent",
                    "task_input": {"location": "Austin"},
                },
            ),
            ToolCall(tool_name="final_answer", arguments={"text": "The weather agent handled it."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Ask weather about Austin."},
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert a2a.calls == [("weather-agent", None)]
    assert "skill" not in a2a.requests[0].metadata
    call_trace = next(t for t in payload["tool_trace"] if t["tool"] == "call_agent")
    assert call_trace["content"]["requested_skill"] is None
    assert call_trace["content"]["chosen_skill"] == "agent/autonomous"


def test_uploaded_image_is_attached_to_geospatial_a2a_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAIN_AGENT_UPLOAD_DIR", str(tmp_path))
    image_path = tmp_path / "flood.jpg"
    image_bytes = b"fake image bytes"
    image_path.write_bytes(image_bytes)
    asyncio.run(_uploaded_image_is_attached_to_geospatial_a2a_call(image_path, image_bytes))


def test_remote_a2a_progress_is_streamed_and_recorded() -> None:
    asyncio.run(_remote_a2a_progress_is_streamed_and_recorded())


async def _remote_a2a_progress_is_streamed_and_recorded() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(_geospatial_card())
    a2a = ProgressA2A()
    streamed: list[dict[str, Any]] = []
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "geospatial-agent",
                    "skill": "geospatial/analyze_image",
                    "task_input": {"request": "Analyze the uploaded image."},
                },
            ),
            ToolCall(tool_name="final_answer", arguments={"text": "Analyzed."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )

    async def progress(entry: dict[str, Any]) -> None:
        streamed.append(entry)

    result = await app.state.runtime.handle_message(
        user_id="u",
        channel=Channel.api,
        text="Analyze this image.",
        progress_callback=progress,
    )

    assert result.state == A2ATaskState.completed
    assert any(entry["tool"] == "remote_status" for entry in streamed)
    assert any(entry["tool"] == "remote_status" for entry in result.tool_trace)


async def _uploaded_image_is_attached_to_geospatial_a2a_call(image_path: Path, image_bytes: bytes) -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(_geospatial_card())
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "geospatial-agent",
                    "skill": "geospatial/analyze_image",
                    "task_input": {"request": "Analyze the uploaded image."},
                },
            ),
            ToolCall(tool_name="final_answer", arguments={"text": "Analyzed."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )

    result = await app.state.runtime.handle_message(
        user_id="u",
        channel=Channel.api,
        text="Analyze this image and tell me what happened.",
        metadata={
            "uploaded_images": [
                {
                    "path": str(image_path),
                    "filename": "flood.jpg",
                    "content_type": "image/jpeg",
                    "size": len(image_bytes),
                }
            ]
        },
    )

    assert result.state == A2ATaskState.completed
    assert a2a.calls == [("geospatial-agent", "geospatial/analyze_image")]
    request = a2a.requests[0]
    image_parts = [part for part in request.messages[0].parts if part.kind == "image"]
    assert len(image_parts) == 1
    assert image_parts[0].content_type == "image/jpeg"
    assert image_parts[0].bytes == base64.b64encode(image_bytes).decode("ascii")
    assert image_parts[0].uri is None
    assert request.metadata["image_transfer"] == "a2a_file_bytes"


def test_compound_workflow_skips_second_main_agent_dispatch() -> None:
    asyncio.run(_compound_workflow_skips_second_dispatch())


async def _compound_workflow_skips_second_dispatch() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(CODING_CARD))
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "coding-agent",
                    "task_input": {
                        "request": "Write a concise Python script that checks whether a number is prime.",
                    },
                },
            ),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {
                        "to": ["bob@example.com"],
                        "subject": "Python script to check if a number is prime",
                        "body": "Send the coding-agent result from the previous observation.",
                    },
                },
            ),
            ToolCall(
                tool_name="final_answer",
                arguments={"text": "The coding agent produced the script and the email agent sent it."},
            ),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={
                "user_id": "u",
                "channel": "api",
                "text": "Write a Python prime-check script and email it to bob@example.com.",
            },
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["state"] == "completed"
    assert a2a.calls == [("coding-agent", None)]
    call_traces = [t for t in payload["tool_trace"] if t["tool"] == "call_agent"]
    assert [t["content"]["agent_id"] for t in call_traces] == ["coding-agent", "email-agent"]
    assert [t["status"] for t in call_traces] == ["completed", "skipped"]
    assert "already dispatched" in call_traces[1]["content"]["reason"]


def test_failed_peer_handoff_allows_remaining_agent_dispatch() -> None:
    asyncio.run(_failed_peer_handoff_allows_remaining_dispatch())


async def _failed_peer_handoff_allows_remaining_dispatch() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(CODING_CARD))
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))

    class PartialCodingA2A(SpyA2A):
        async def send_task(self, agent, request):
            if agent.agent_id != "coding-agent":
                return await super().send_task(agent, request)
            self.calls.append((agent.agent_id, request.skill))
            self.requests.append(request)
            out = {
                "skill": "agent/autonomous",
                "chosen_skill": "agent/autonomous",
                "summary": "Created prime_check.py, but email delivery failed.",
                "generated_files": [
                    {
                        "path": "prime_check.py",
                        "content": "def is_prime(n):\n    return n > 1\n",
                    }
                ],
                "delivery_results": [
                    {
                        "status": "send_failed",
                        "kind": "email",
                        "skill_query": "email/send",
                        "error": "ModuleNotFoundError: email agent import failed",
                    }
                ],
            }
            return A2ATaskResult(
                task_id=request.task_id or "task-coding-partial",
                context_id=request.context_id,
                state=A2ATaskState.completed,
                messages=[
                    A2AMessage(role="agent", parts=[A2APart(kind="text", text=out["summary"])])
                ],
                structured_output=out,
            )

    a2a = PartialCodingA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "coding-agent",
                    "task_input": {
                        "request": "Write a Python prime checker and email it to bob@example.com.",
                    },
                },
            ),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {
                        "to": ["bob@example.com"],
                        "subject": "Python prime checker",
                        "body": "Here is prime_check.py from the coding-agent output.",
                    },
                },
            ),
            ToolCall(tool_name="final_answer", arguments={"text": "Generated and emailed."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={
                "user_id": "u",
                "channel": "api",
                "text": "Write a Python prime checker and email it to bob@example.com.",
            },
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["state"] == "completed"
    assert a2a.calls == [("coding-agent", None), ("email-agent", "email/send")]
    call_traces = [t for t in payload["tool_trace"] if t["tool"] == "call_agent"]
    assert [t["status"] for t in call_traces] == ["completed", "completed"]


def test_strict_mode_write_skill_requires_confirmation_then_resumes() -> None:
    asyncio.run(_strict_write_requires_confirmation())


async def _strict_write_requires_confirmation() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SpyA2A()
    store = InMemoryStateStore()
    llm = ScriptedLLMClient(
        [
            # First message: model wants to send.
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {
                        "to": ["bob@example.com"],
                        "subject": "Hello",
                        "body": "Test",
                    },
                },
            ),
        ]
    )
    app = create_app(
        config=_config(policy="strict"),
        directory_client=directory,
        a2a_client=a2a,
        state_store=store,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Send an email to bob@example.com."},
        )
        first_payload = first.json()
        assert first_payload["confirmation_required"] is True
        assert first_payload["pending_confirmation"]["skill"] == "email/send"
        assert a2a.calls == []  # not yet invoked

        # Second message: user says "yes".
        second = await client.post(
            "/messages",
            json={
                "user_id": "u",
                "channel": "api",
                "context_id": first_payload["context_id"],
                "text": "yes",
            },
        )
    second_payload = second.json()
    assert second_payload["confirmation_required"] is False
    assert a2a.calls == [("email-agent", "email/send")]
    assert a2a.requests[0].metadata["confirmed"] is True
    assert second_payload["structured_outputs"]["email/send"]["sent_email"]["subject"] == "Hello"


def test_call_unknown_agent_returns_observation_error_not_500() -> None:
    asyncio.run(_call_unknown_agent_observation_error())


async def _call_unknown_agent_observation_error() -> None:
    llm = ScriptedLLMClient(
        [
            ToolCall(
                tool_name="call_agent",
                arguments={"agent_id": "ghost-agent", "skill": "x/y", "task_input": {}},
            ),
            ToolCall(tool_name="final_answer", arguments={"text": "No agent with that id."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Use the ghost agent."},
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    err_trace = next(t for t in payload["tool_trace"] if t["tool"] == "call_agent")
    assert err_trace["status"] == "error"
    assert "not in ADS" in err_trace["content"]["error"] or "not advertised" in err_trace["content"]["error"]


def test_bad_skill_hint_does_not_block_valid_agent_call() -> None:
    asyncio.run(_bad_skill_hint_allowed())


async def _bad_skill_hint_allowed() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "not/a-real-skill",
                    "task_input": {"subject": "Hi"},
                },
            ),
            ToolCall(tool_name="final_answer", arguments={"text": "The email agent handled it."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Ask the email agent to help."},
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert a2a.calls == [("email-agent", "not/a-real-skill")]
    assert a2a.requests[0].metadata["skill"] == "not/a-real-skill"
    assert a2a.requests[0].metadata["requested_skill"] == "not/a-real-skill"
    call_trace = next(t for t in payload["tool_trace"] if t["tool"] == "call_agent")
    assert call_trace["status"] == "completed"
    assert call_trace["content"]["requested_skill"] == "not/a-real-skill"
    assert call_trace["content"]["chosen_skill"] == "agent/autonomous"


def test_followup_can_see_prior_assistant_message_in_history() -> None:
    asyncio.run(_followup_sees_prior_assistant_in_history())


def test_server_reuses_one_context_for_requests_without_context_id() -> None:
    asyncio.run(_server_reuses_one_context_without_context_id())


async def _server_reuses_one_context_without_context_id() -> None:
    store = InMemoryStateStore()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="final_answer", arguments={"text": "First answer."}),
            ToolCall(tool_name="final_answer", arguments={"text": "Second answer."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        state_store=store,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "First request."},
        )
        second = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Second request."},
        )

    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["context_id"] == second_payload["context_id"]
    assert store.server_context_id == first_payload["context_id"]
    assert list(store.contexts) == [first_payload["context_id"]]
    history = "\n".join(store.get_context(first_payload["context_id"]).conversation_history)
    assert "user: First request." in history
    assert "assistant: First answer." in history
    assert "user: Second request." in history
    assert "assistant: Second answer." in history


async def _followup_sees_prior_assistant_in_history() -> None:
    llm = ScriptedLLMClient(
        [
            ToolCall(
                tool_name="final_answer",
                arguments={"text": 'Here is a hello world program:\n```python\nprint("hi")\n```'},
            ),
            ToolCall(
                tool_name="final_answer",
                arguments={"text": "I can see the prior program in history."},
            ),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Write a hello world program."},
        )
        ctx = first.json()["context_id"]
        await client.post(
            "/messages",
            json={
                "user_id": "u",
                "channel": "api",
                "context_id": ctx,
                "text": "Send this program to bob@example.com.",
            },
        )
    # The second turn's TurnState given to the LLM must contain the assistant's prior reply.
    second_state = llm.states[1]
    history = "\n".join(second_state.conversation_history)
    assert "assistant: Here is a hello world program" in history
    assert 'print("hi")' in history


def test_negative_confirmation_cancels_without_invoking_a2a() -> None:
    asyncio.run(_negative_cancel())


async def _negative_cancel() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {"to": ["x@y.com"], "subject": "s", "body": "b"},
                },
            ),
        ]
    )
    app = create_app(
        config=_config(policy="strict"),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Send the email."},
        )
        ctx = first.json()["context_id"]
        cancel = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "context_id": ctx, "text": "no"},
        )
    assert a2a.calls == []
    assert cancel.json()["state"] == "canceled"


def test_dynamic_agent_works_without_pi_code_change() -> None:
    asyncio.run(_dynamic_agent_no_code_change())


async def _dynamic_agent_no_code_change() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(_disaster_card())
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(tool_name="describe_agent", arguments={"agent_id": "disaster-agent"}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "disaster-agent",
                    "skill": "disaster/tornado_plan",
                    "task_input": {"region": "Texas"},
                },
            ),
            ToolCall(
                tool_name="final_answer",
                arguments={"text": "Here's the tornado plan from the disaster agent."},
            ),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Draft a tornado plan."},
        )
    payload = resp.json()
    assert a2a.calls == [("disaster-agent", "disaster/tornado_plan")]
    assert payload["selected_agent"] == "disaster-agent"


def test_empty_task_input_for_email_send_is_augmented_from_user_text() -> None:
    asyncio.run(_empty_task_input_augmented())


async def _empty_task_input_augmented() -> None:
    """Some LLMs leave task_input empty and rely on the free-text prompt.
    The runtime must extract to/subject/body so email-agent doesn't bounce."""
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {},  # ← LLM forgot to fill it
                },
            ),
            ToolCall(tool_name="final_answer", arguments={"text": "Sent."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        await client.post(
            "/messages",
            json={
                "user_id": "u",
                "channel": "api",
                "text": 'Send an email to xjian@wustl.edu, subject is "Test title", body is "test content".',
            },
        )
    request = a2a.requests[0]
    assert request.metadata["to"] == ["xjian@wustl.edu"]
    assert request.metadata["subject"] == "Test title"
    assert request.metadata["body"] == "test content"


def test_confirmation_required_response_includes_full_tool_trace() -> None:
    """Bug 2: the TurnResult on the confirmation_required path must carry the
    accumulated tool_trace, not [].
    """
    asyncio.run(_confirmation_trace_carried())


async def _confirmation_trace_carried() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {"to": ["x@y.com"], "subject": "s", "body": "b"},
                },
            ),
        ]
    )
    app = create_app(
        config=_config(policy="strict"),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Send the email."},
        )
    payload = resp.json()
    assert payload["confirmation_required"] is True
    tool_names = [t["tool"] for t in payload["tool_trace"]]
    assert tool_names == ["list_agents", "call_agent"], tool_names
    call_trace = payload["tool_trace"][1]
    assert call_trace["status"] == "confirmation_required"


def test_failed_a2a_can_be_retried_in_same_turn() -> None:
    """Bug 3: dedup must not block retries after a failed A2A call."""
    asyncio.run(_retry_after_failure())


async def _retry_after_failure() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))

    class FlakyA2A(A2AClient):
        def __init__(self):
            self.calls = 0

        async def send_task(self, agent, request):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("simulated transient timeout")
            out = {"skill": request.skill, "summary": "Sent on retry."}
            return A2ATaskResult(
                task_id=request.task_id or "task-retry",
                context_id=request.context_id,
                state=A2ATaskState.completed,
                messages=[A2AMessage(role="agent", parts=[A2APart(kind="text", text="ok")])],
                structured_output=out,
            )

    flaky = FlakyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {"to": ["x@y.com"], "subject": "s", "body": "b"},
                },
            ),
            # First call timed out; the LLM tries again with the same arguments.
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {"to": ["x@y.com"], "subject": "s", "body": "b"},
                },
            ),
            ToolCall(tool_name="final_answer", arguments={"text": "Sent."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=flaky,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Send the email."},
        )
    payload = resp.json()
    assert flaky.calls == 2, "flaky A2A must have been called twice (initial fail + retry)"
    statuses = [t["status"] for t in payload["tool_trace"]]
    # First call_agent → failed; second call_agent → completed (NOT skipped).
    assert "failed" in statuses
    assert "completed" in statuses


def test_resume_confirmation_writes_user_and_assistant_history() -> None:
    """Bug 5: replying 'yes' must add user and assistant turns to history so
    later turns can see the confirmation happened.
    """
    asyncio.run(_resume_history())


async def _resume_history() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SpyA2A()
    state_store = InMemoryStateStore()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {"to": ["x@y.com"], "subject": "s", "body": "b"},
                },
            ),
        ]
    )
    app = create_app(
        config=_config(policy="strict"),
        directory_client=directory,
        a2a_client=a2a,
        state_store=state_store,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Send the email."},
        )
        ctx = first.json()["context_id"]
        await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "context_id": ctx, "text": "yes"},
        )
    history = state_store.get_context(ctx).conversation_history
    assert any("user: yes" in h for h in history), history
    assert any(h.startswith("assistant:") for h in history), history


def test_reasoning_summary_surfaces_in_tool_trace() -> None:
    """Reasoning text from the LLM must appear as a separate 'reasoning' entry
    in tool_trace before the tool that the LLM chose."""
    asyncio.run(_reasoning_in_trace())


async def _reasoning_in_trace() -> None:
    llm = ScriptedLLMClient(
        [
            ToolCall(
                tool_name="final_answer",
                arguments={"text": "Hi."},
                reasoning="I should greet the user briefly.",
            ),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Hello?"},
        )
    payload = resp.json()
    tools = [t["tool"] for t in payload["tool_trace"]]
    assert tools == ["reasoning", "final_answer"], tools
    reasoning_entry = payload["tool_trace"][0]
    assert reasoning_entry["content"]["text"] == "I should greet the user briefly."
    assert reasoning_entry["content"]["next_tool"] == "final_answer"


def test_openai_chain_state_is_per_turn_not_per_client() -> None:
    """Bug 1: the OpenAI response_id chain must live on TurnState, not on the
    client instance, so concurrent /messages requests cannot interleave each
    other's chains.
    """
    from v3_main_agent.main_agent.llm_client import OpenAIToolLoopClient
    client = OpenAIToolLoopClient(_config())
    # The client instance must NOT carry any per-request chain state.
    assert not hasattr(client, "_previous_response_id")
    assert not hasattr(client, "_last_call_id")


def test_duplicate_call_agent_in_same_turn_is_skipped_not_re_invoked() -> None:
    asyncio.run(_dedupe_repeated_call_agent())


async def _dedupe_repeated_call_agent() -> None:
    """gpt-4o-mini sometimes loops on side-effecting skills. Verify the runtime
    only invokes A2A once per (agent_id, skill) per turn, even if the LLM
    insists on calling call_agent again."""
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    a2a = SpyA2A()
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {"to": ["x@y.com"], "subject": "hi", "body": "test"},
                },
            ),
            # LLM tries again with the SAME (agent, skill).
            ToolCall(
                tool_name="call_agent",
                arguments={
                    "agent_id": "email-agent",
                    "skill": "email/send",
                    "task_input": {"to": ["x@y.com"], "subject": "hi", "body": "test"},
                },
            ),
            ToolCall(tool_name="final_answer", arguments={"text": "Sent (once)."}),
        ]
    )
    app = create_app(
        config=_config(),  # permissive
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Send the email."},
        )
    payload = resp.json()
    # Only ONE A2A call must have hit the network.
    assert a2a.calls == [("email-agent", "email/send")], a2a.calls
    # The third tool_trace entry (the duplicate call) must be marked skipped.
    duplicate_trace = payload["tool_trace"][2]
    assert duplicate_trace["tool"] == "call_agent"
    assert duplicate_trace["status"] == "skipped"
    assert "already ran" in duplicate_trace["content"]["reason"]


def test_duplicate_no_skill_call_agent_in_same_turn_is_skipped() -> None:
    asyncio.run(_dedupe_repeated_call_agent_without_skill())


async def _dedupe_repeated_call_agent_without_skill() -> None:
    directory = SpyDirectory(fake_ads=True)
    directory.publish_agent_card(load_agent_card(WEATHER_CARD))
    a2a = SpyA2A()
    repeated_args = {
        "agent_id": "weather-agent",
        "task_input": {"location": "Austin"},
    }
    llm = ScriptedLLMClient(
        [
            ToolCall(tool_name="list_agents", arguments={}),
            ToolCall(tool_name="call_agent", arguments=repeated_args),
            ToolCall(tool_name="call_agent", arguments=repeated_args),
            ToolCall(tool_name="final_answer", arguments={"text": "Handled once."}),
        ]
    )
    app = create_app(
        config=_config(),
        directory_client=directory,
        a2a_client=a2a,
        llm_client=llm,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/messages",
            json={"user_id": "u", "channel": "api", "text": "Ask weather about Austin."},
        )
    payload = resp.json()
    assert a2a.calls == [("weather-agent", None)]
    duplicate_trace = payload["tool_trace"][2]
    assert duplicate_trace["tool"] == "call_agent"
    assert duplicate_trace["status"] == "skipped"
    assert duplicate_trace["content"]["requested_skill"] is None


def _disaster_card() -> AgentCard:
    return AgentCard(
        name="Disaster Agent",
        description="Disaster response planning agent.",
        url="http://10.0.0.42:9200/",
        provider=AgentProvider(organization="External Disaster Org", url="https://example.invalid"),
        version="0.1.0",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        defaultInputModes=["text/plain", "application/json"],
        defaultOutputModes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="disaster/tornado_plan",
                name="Tornado Plan",
                description="Produce a tornado response plan for a region.",
                tags=["disaster/tornado_plan", "disaster", "tornado", "plan"],
                inputModes=["text/plain", "application/json"],
                outputModes=["text/plain", "application/json"],
            )
        ],
    )


def _geospatial_card() -> AgentCard:
    return AgentCard(
        name="GeoSpatial Agent",
        description="Infer likely location from an uploaded photo and describe visible incidents.",
        url="http://10.0.0.42:9105/",
        provider=AgentProvider(organization="External Geo Org", url="https://example.invalid"),
        version="0.1.0",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        defaultInputModes=["text/plain", "application/json", "image/jpeg"],
        defaultOutputModes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="geospatial/analyze_image",
                name="Analyze Image",
                description="Geolocate an image and assess visible public-safety context.",
                tags=["geospatial/analyze_image", "image/geospatial_analysis"],
                inputModes=["text/plain", "application/json", "image/jpeg"],
                outputModes=["text/plain", "application/json"],
            )
        ],
    )
