from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.common.models import (
    A2AArtifact,
    A2AMessage,
    A2APart,
    A2ATaskResult,
    A2ATaskRequest,
    A2ATaskState,
    AgentCapability,
    AgentInvocationRecord,
    Channel,
    DiscoveredAgent,
    PIContext,
    PolicyDecision,
    UserMessage,
)
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.policy import evaluate_skill
from v2_agent_network.services.pi_agent.router import EMAIL_SEND_SKILL, plan_workflow, select_agent
from v2_agent_network.services.state.store import InMemoryStateStore

CARD_FILES = {
    "weather": "weather_agent.card.json",
    "calendar": "calendar_agent.card.json",
    "email": "email_agent.card.json",
    "photo_geospatial": "photo_geospatial_agent.card.json",
    "intelligence": "intelligence_agent.card.json",
    "coding": "coding_agent.card.json",
}


class IncomingMessage(BaseModel):
    user_id: str = Field(default="demo-user")
    channel: Channel = Channel.api
    text: str
    context_id: str | None = None
    requested_skill: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def create_app(
    *,
    directory_client: DirectoryClient | None = None,
    a2a_client: A2AClient | None = None,
    state_store: InMemoryStateStore | None = None,
    publish_weather_card: bool = True,
    publish_calendar_email_cards: bool = False,
    publish_geo_intelligence_cards: bool = False,
    publish_coding_card: bool = False,
    agent_card_paths: Sequence[str | Path] | None = None,
) -> FastAPI:
    directory = directory_client or DirectoryClient()
    client = a2a_client or A2AClient()
    store = state_store or InMemoryStateStore()
    app = FastAPI(title="V2 PI Agent", version="0.1.0")
    app.state.directory_client = directory
    app.state.a2a_client = client
    app.state.state_store = store

    if publish_weather_card:
        _publish_card(directory, CARD_FILES["weather"])
    if publish_calendar_email_cards:
        _publish_card(directory, CARD_FILES["calendar"])
        _publish_card(directory, CARD_FILES["email"])
    if publish_geo_intelligence_cards:
        _publish_card(directory, CARD_FILES["photo_geospatial"])
        _publish_card(directory, CARD_FILES["intelligence"])
    if publish_coding_card:
        _publish_card(directory, CARD_FILES["coding"])
    for card_path in _configured_agent_card_paths(agent_card_paths, allow_local_cards=directory.fake_ads):
        directory.publish_agent_card(load_agent_card(card_path))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/messages")
    async def post_message(incoming: IncomingMessage) -> dict[str, object]:
        try:
            user_message = UserMessage(
                channel=incoming.channel,
                sender_id=incoming.user_id,
                text=incoming.text,
            )
            context = PIContext(
                context_id=incoming.context_id or str(uuid4()),
                user_id=incoming.user_id,
                channel=incoming.channel,
                original_message_id=user_message.id,
                current_goal=incoming.text,
                conversation_history=[incoming.text],
            )
            store.save_context(context)

            if incoming.requested_skill and _looks_like_endpoint_url(incoming.requested_skill):
                raise HTTPException(
                    status_code=400,
                    detail="requested_skill must be a skill name, not an endpoint URL.",
                )
            workflow_skills = plan_workflow(incoming.text, requested_skill=incoming.requested_skill)
            results: list[A2ATaskResult] = []
            selected_steps: list[dict[str, str | None]] = []
            policy_decisions: list[dict[str, str]] = []
            artifacts: list[A2AArtifact] = []
            task_ids: dict[str, str] = {}
            previous_outputs: dict[str, object] = {}
            source_artifacts: list[dict[str, str]] = []
            available_slots: list[dict[str, str]] | None = None
            draft_email: dict[str, str] | None = None
            image_assessment: dict[str, object] | None = None
            weather_forecast: dict[str, object] | None = None
            situation_summary: dict[str, object] | None = None
            repo_summary: str | None = None
            detected_issues: list[object] = []
            recommended_next_task: str | None = None
            risk_level: str | None = None
            proposed_action: dict[str, object] | None = None
            pending_confirmation: dict[str, str] | None = None

            for skill in workflow_skills:
                try:
                    routing = select_agent(
                        context_id=context.context_id,
                        user_goal=incoming.text,
                        directory_client=directory,
                        skill=skill,
                    )
                except LookupError as exc:
                    if incoming.requested_skill:
                        return _no_capable_agent_response(
                            context=context,
                            workflow_skills=workflow_skills,
                            requested_skill=incoming.requested_skill,
                            reason=str(exc),
                        )
                    raise
                selected_agent = routing.selected_agents[0]
                selected_capability = _capability_for_skill(selected_agent, skill)
                policy_decision = evaluate_skill(skill, selected_capability, metadata=incoming.metadata)
                policy_decisions.append(_policy_entry(skill, selected_agent, policy_decision))
                if policy_decision.status.value == "denied":
                    raise HTTPException(status_code=403, detail=policy_decision.reason)
                if policy_decision.status.value == "requires_confirmation":
                    pending_confirmation = _pending_confirmation(skill, selected_agent, policy_decision)
                    proposed_action = _proposed_action(skill, incoming.text)
                    risk_level = "medium"
                    selected_steps.append(
                        {"skill": skill, "agent_id": selected_agent.agent_id, "task_id": None}
                    )
                    continue

                task_request = _task_request(
                    incoming=incoming,
                    context=context,
                    selected_agent=selected_agent,
                    skill=skill,
                    policy_decision=policy_decision,
                    available_slots=available_slots,
                    previous_outputs=previous_outputs,
                    source_artifacts=source_artifacts,
                    reference_task_ids=list(task_ids.values()),
                )
                result = await client.send_task(selected_agent, task_request)
                results.append(result)
                artifacts.extend(result.artifacts)
                task_ids[skill] = result.task_id
                if result.structured_output:
                    previous_outputs[skill] = result.structured_output
                source_artifacts.extend(_artifact_references(result, skill))
                selected_steps.append(
                    {"skill": skill, "agent_id": selected_agent.agent_id, "task_id": result.task_id}
                )
                store.save_invocation(
                    AgentInvocationRecord(
                        context_id=context.context_id,
                        task_id=result.task_id,
                        agent_id=selected_agent.agent_id,
                        skill=task_request.skill,
                        request_summary=incoming.text,
                        status=result.state,
                        artifacts=result.artifacts,
                        policy_decision=policy_decision,
                        selected_agent=selected_agent,
                        completed_at=result.messages[-1].created_at if result.messages else None,
                        error=result.error,
                    )
                )
                context.active_tasks.append(result.task_id)
                store.save_context(context)

                slots = _extract_available_slots(result)
                if slots:
                    available_slots = slots
                extracted_draft = _extract_draft_email(result)
                if extracted_draft:
                    draft_email = extracted_draft
                extracted_image_assessment = _extract_image_assessment(result)
                if extracted_image_assessment:
                    image_assessment = extracted_image_assessment
                extracted_weather = _extract_weather_forecast(result)
                if extracted_weather:
                    weather_forecast = extracted_weather
                extracted_situation = _extract_situation_summary(result)
                if extracted_situation:
                    situation_summary = extracted_situation
                extracted_coding = _extract_coding_output(result)
                if extracted_coding:
                    repo_summary = extracted_coding.get("repo_summary")
                    detected_issues = extracted_coding.get("detected_issues") or []
                    recommended_next_task = extracted_coding.get("recommended_next_task")
                    risk_level = extracted_coding.get("risk_level")
                    if isinstance(extracted_coding.get("proposed_action"), dict):
                        proposed_action = extracted_coding["proposed_action"]

            if draft_email and pending_confirmation is None:
                email_agent = _agent_for_skill(selected_steps, "email/draft")
                send_policy = evaluate_skill(EMAIL_SEND_SKILL)
                policy_decisions.append(
                    {
                        "skill": EMAIL_SEND_SKILL,
                        "agent_id": email_agent,
                        "policy_decision": send_policy.status.value,
                        "confirmation_status": send_policy.confirmation_status.value,
                    }
                )
                pending_confirmation = {
                    "skill": EMAIL_SEND_SKILL,
                    "agent_id": email_agent,
                    "policy_decision": send_policy.status.value,
                    "confirmation_status": send_policy.confirmation_status.value,
                    "reason": send_policy.reason,
                }

            last_result = results[-1] if results else None
            overall_policy = (
                pending_confirmation["policy_decision"]
                if pending_confirmation
                else policy_decisions[-1]["policy_decision"]
            )
            overall_confirmation = (
                pending_confirmation["confirmation_status"]
                if pending_confirmation
                else policy_decisions[-1]["confirmation_status"]
            )

            return {
                "context_id": context.context_id,
                "task_id": last_result.task_id if last_result else None,
                "task_ids": task_ids,
                "state": last_result.state if last_result else A2ATaskState.input_required,
                "selected_agent": selected_steps[-1]["agent_id"] if selected_steps else None,
                "selected_agents": selected_steps,
                "workflow": workflow_skills,
                "policy_decision": overall_policy,
                "policy_decisions": policy_decisions,
                "confirmation_status": overall_confirmation,
                "confirmation_required": pending_confirmation is not None,
                "pending_confirmation": pending_confirmation,
                "text": _last_text(last_result) if last_result else "",
                "available_slots": available_slots or [],
                "draft_email": draft_email,
                "image_assessment": image_assessment,
                "weather_forecast": weather_forecast,
                "situation_summary": situation_summary,
                "key_risks": _list_from_situation(situation_summary, "key_risks"),
                "recommended_plan": _list_from_situation(situation_summary, "recommended_plan"),
                "assumptions": _list_from_situation(situation_summary, "assumptions"),
                "repo_summary": repo_summary,
                "detected_issues": detected_issues,
                "recommended_next_task": recommended_next_task,
                "risk_level": risk_level,
                "proposed_action": proposed_action,
                "source_artifacts": source_artifacts,
                "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
                "structured_output": last_result.structured_output if last_result else None,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app


def _publish_card(directory: DirectoryClient, filename: str) -> None:
    card_path = Path(__file__).parents[2] / "configs" / "agent_cards" / filename
    directory.publish_agent_card(load_agent_card(card_path))


def _configured_agent_card_paths(
    agent_card_paths: Sequence[str | Path] | None,
    *,
    allow_local_cards: bool,
) -> list[Path]:
    if agent_card_paths is not None:
        paths = [Path(path) for path in agent_card_paths]
        if paths and not allow_local_cards:
            raise RuntimeError(
                "PI Agent Card path loading is only supported in fake ADS mode. "
                "Publish Agent Cards to real ADS and let PI discover them through DirectoryClient."
            )
        return paths
    if not allow_local_cards:
        return []
    raw_paths = os.getenv("PI_AGENT_CARD_PATHS", "")
    return [Path(item.strip()) for item in raw_paths.split(",") if item.strip()]


def _looks_like_endpoint_url(value: str) -> bool:
    parsed = urlsplit(value)
    if bool(parsed.scheme and parsed.netloc) or "://" in value:
        return True
    host_like_prefix = value.split("/", 1)[0].lower()
    if host_like_prefix in {"localhost"}:
        return True
    if host_like_prefix.startswith(("127.", "0.0.0.0", "[::1]")):
        return True
    return ":" in host_like_prefix or "." in host_like_prefix


def _no_capable_agent_response(
    *,
    context: PIContext,
    workflow_skills: list[str],
    requested_skill: str,
    reason: str,
) -> dict[str, object]:
    return {
        "context_id": context.context_id,
        "task_id": None,
        "task_ids": {},
        "state": A2ATaskState.input_required,
        "selected_agent": None,
        "selected_agents": [],
        "workflow": workflow_skills,
        "policy_decision": "denied",
        "policy_decisions": [],
        "confirmation_status": "not_required",
        "confirmation_required": False,
        "pending_confirmation": None,
        "text": f"no capable agent found for requested_skill {requested_skill}",
        "available_slots": [],
        "draft_email": None,
        "image_assessment": None,
        "weather_forecast": None,
        "situation_summary": None,
        "key_risks": [],
        "recommended_plan": [],
        "assumptions": [],
        "repo_summary": None,
        "detected_issues": [],
        "recommended_next_task": None,
        "risk_level": None,
        "proposed_action": None,
        "source_artifacts": [],
        "artifacts": [],
        "structured_output": None,
        "no_capable_agent_found": True,
        "reason": reason,
    }


def _capability_for_skill(agent: DiscoveredAgent, skill: str) -> AgentCapability:
    for capability in agent.skills:
        official_names = {item.name for item in capability.official_oasf_skills}
        if skill in set(capability.skill_tags) | set(capability.custom_skill_tags) | official_names:
            return capability
    return agent.skills[0]


def _task_request(
    *,
    incoming: IncomingMessage,
    context: PIContext,
    selected_agent: DiscoveredAgent,
    skill: str,
    policy_decision: PolicyDecision,
    available_slots: list[dict[str, str]] | None,
    previous_outputs: dict[str, object],
    source_artifacts: list[dict[str, str]],
    reference_task_ids: list[str],
) -> A2ATaskRequest:
    parts = [A2APart(kind="text", text=incoming.text)]
    metadata = {
        "user_id": incoming.user_id,
        "channel": incoming.channel.value,
        "policy_decision": policy_decision.status.value,
        "confirmation_status": policy_decision.confirmation_status.value,
    }
    task_metadata = _safe_user_metadata(incoming.metadata)
    if skill == "email/draft":
        task_metadata.setdefault("create_gmail_draft", False)
    metadata.update(task_metadata)
    if skill == "image/geospatial_analysis":
        image_uri = "file://mock/geospatial-photo-austin.jpg"
        parts.append(
            A2APart(
                kind="image",
                content_type="image/jpeg",
                uri=image_uri,
                metadata={"name": "geospatial-photo-austin.jpg"},
            )
        )
        metadata["image_uri"] = image_uri
    if available_slots:
        parts.append(A2APart(kind="data", data={"available_slots": available_slots}))
        metadata["available_slots"] = available_slots
    if previous_outputs or source_artifacts:
        previous_outputs_snapshot = deepcopy(previous_outputs)
        source_artifacts_snapshot = [dict(item) for item in source_artifacts]
        workflow_context = {
            "previous_outputs": previous_outputs_snapshot,
            "source_artifacts": source_artifacts_snapshot,
        }
        parts.append(A2APart(kind="data", data=workflow_context))
        metadata.update(workflow_context)
    if reference_task_ids:
        metadata["reference_task_ids"] = reference_task_ids
    return A2ATaskRequest(
        task_id=str(uuid4()),
        context_id=context.context_id,
        target_agent_id=selected_agent.agent_id,
        skill=skill,
        user_goal=incoming.text,
        messages=[A2AMessage(role="user", parts=parts)],
        metadata=metadata,
    )


def _safe_user_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    reserved = {
        "user_id",
        "channel",
        "policy_decision",
        "confirmation_status",
        "confirmed",
        "skill",
    }
    return {key: value for key, value in metadata.items() if key not in reserved}


def _policy_entry(
    skill: str,
    agent: DiscoveredAgent,
    policy_decision: PolicyDecision,
) -> dict[str, str]:
    return {
        "skill": skill,
        "agent_id": agent.agent_id,
        "policy_decision": policy_decision.status.value,
        "confirmation_status": policy_decision.confirmation_status.value,
    }


def _pending_confirmation(
    skill: str,
    agent: DiscoveredAgent,
    policy_decision: PolicyDecision,
) -> dict[str, str]:
    return {
        "skill": skill,
        "agent_id": agent.agent_id,
        "policy_decision": policy_decision.status.value,
        "confirmation_status": policy_decision.confirmation_status.value,
        "reason": policy_decision.reason,
    }


def _agent_for_skill(selected_steps: list[dict[str, str | None]], skill: str) -> str | None:
    for step in selected_steps:
        if step["skill"] == skill:
            return step["agent_id"]
    return None


def _extract_available_slots(result: A2ATaskResult) -> list[dict[str, str]] | None:
    output = result.structured_output or {}
    value = output.get("available_slots")
    if isinstance(value, list):
        return value
    return None


def _extract_draft_email(result: A2ATaskResult) -> dict[str, str] | None:
    output = result.structured_output or {}
    value = output.get("draft_email")
    if isinstance(value, dict):
        return value
    return None


def _extract_image_assessment(result: A2ATaskResult) -> dict[str, object] | None:
    output = result.structured_output or {}
    value = output.get("image_assessment")
    if isinstance(value, dict):
        return value
    return None


def _extract_weather_forecast(result: A2ATaskResult) -> dict[str, object] | None:
    output = result.structured_output or {}
    if output.get("skill") == "weather/forecast":
        return output
    return None


def _extract_situation_summary(result: A2ATaskResult) -> dict[str, object] | None:
    output = result.structured_output or {}
    if "situation_summary" in output:
        return output
    return None


def _extract_coding_output(result: A2ATaskResult) -> dict[str, object] | None:
    output = result.structured_output or {}
    if str(output.get("skill", "")).startswith("code/"):
        return output
    return None


def _proposed_action(skill: str, user_goal: str) -> dict[str, object] | None:
    if skill == "code/bug_fix":
        return {
            "skill": skill,
            "summary": "Prepare a minimal bug-fix patch plan for the failing test.",
            "requested_goal": user_goal,
            "steps": [
                "Inspect the failing test and nearby implementation.",
                "Draft the smallest repository change needed to address the failure.",
                "Run the focused test before broader regression tests.",
            ],
            "modifies_repository": True,
        }
    if skill == "code/test_generation":
        return {
            "skill": skill,
            "summary": "Prepare test-generation changes for review before writing files.",
            "requested_goal": user_goal,
            "steps": [
                "Identify behavior needing coverage.",
                "Draft proposed test cases and target files.",
                "Wait for confirmation before writing tests.",
            ],
            "modifies_repository": True,
        }
    return None


def _artifact_references(result: A2ATaskResult, skill: str) -> list[dict[str, str]]:
    return [
        {
            "skill": skill,
            "task_id": result.task_id,
            "artifact_id": artifact.artifact_id,
            "name": artifact.name,
        }
        for artifact in result.artifacts
    ]


def _list_from_situation(
    situation_summary: dict[str, object] | None,
    key: str,
) -> list[object]:
    if not situation_summary:
        return []
    value = situation_summary.get(key)
    return value if isinstance(value, list) else []


def _last_text(result: A2ATaskResult | None) -> str:
    if result is None:
        return ""
    for message in reversed(result.messages):
        for part in message.parts:
            if part.kind == "text" and part.text:
                return part.text
    if result.state != A2ATaskState.completed and result.error:
        return result.error
    return ""


app = create_app(
    publish_weather_card=os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true",
    publish_calendar_email_cards=os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true",
    publish_geo_intelligence_cards=os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true",
    publish_coding_card=os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true",
)
