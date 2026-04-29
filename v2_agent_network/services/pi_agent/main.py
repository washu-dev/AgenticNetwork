from __future__ import annotations

import os
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
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
    ConfirmationStatus,
    DiscoveredAgent,
    PIContext,
    PolicyDecision,
    PolicyDecisionStatus,
    UserMessage,
    utc_now,
)
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.agent_runtime import PIAgentRuntime, ToolLoopLLMClient
from v2_agent_network.services.pi_agent.llm_config import (
    PILLMConfig,
    PILLMConfigError,
    load_pi_llm_config,
)
from v2_agent_network.services.pi_agent.llm_planner import PILLMPlanner, PILLMPlannerError
from v2_agent_network.services.pi_agent.policy import evaluate_skill
from v2_agent_network.services.pi_agent.router import (
    EMAIL_DRAFT_SKILL,
    EMAIL_SEND_SKILL,
    plan_workflow,
    select_agent,
)
from v2_agent_network.services.pi_agent.skill_catalog import build_skill_catalog
from v2_agent_network.services.state.store import InMemoryStateStore

logger = logging.getLogger(__name__)

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


class ConfirmationRequest(BaseModel):
    user_id: str = Field(default="demo-user")
    channel: Channel = Channel.api
    context_id: str
    skill: str | None = None
    confirmed: bool = False
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
    llm_config: PILLMConfig | None = None,
    llm_planner: PILLMPlanner | None = None,
    agent_mode: str | None = None,
    agent_runtime: PIAgentRuntime | None = None,
    tool_loop_llm_client: ToolLoopLLMClient | None = None,
) -> FastAPI:
    directory = directory_client or DirectoryClient()
    client = a2a_client or A2AClient()
    store = state_store or InMemoryStateStore()
    planner_config = llm_config or load_pi_llm_config()
    configured_agent_mode = (agent_mode or os.getenv("PI_AGENT_MODE", "tool_loop")).strip().lower()
    use_tool_loop = (
        configured_agent_mode == "tool_loop"
        and planner_config.enabled
        and llm_planner is None
    )
    planner = (
        None
        if use_tool_loop
        else llm_planner or (PILLMPlanner(planner_config) if planner_config.enabled else None)
    )
    runtime = agent_runtime
    if use_tool_loop and runtime is None:
        runtime = PIAgentRuntime(
            directory_client=directory,
            a2a_client=client,
            state_store=store,
            llm_config=planner_config,
            llm_client=tool_loop_llm_client,
        )
    app = FastAPI(title="V2 PI Agent", version="0.1.0")
    app.state.directory_client = directory
    app.state.a2a_client = client
    app.state.state_store = store
    app.state.llm_config = planner_config
    app.state.llm_planner = planner
    app.state.agent_mode = "tool_loop" if use_tool_loop else "legacy"
    app.state.agent_runtime = runtime
    logger.info(
        "pi_app_created fake_ads=%s llm_enabled=%s llm_model=%s agent_mode=%s",
        directory.fake_ads,
        planner_config.enabled,
        planner_config.model if planner_config.enabled else None,
        app.state.agent_mode,
    )

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

    @app.get("/", response_class=HTMLResponse)
    def chat_ui() -> HTMLResponse:
        return HTMLResponse(_CHAT_UI_HTML)

    @app.get("/chat", response_class=HTMLResponse)
    def chat_ui_alias() -> HTMLResponse:
        return HTMLResponse(_CHAT_UI_HTML)

    @app.post("/messages")
    async def post_message(incoming: IncomingMessage) -> dict[str, object]:
        try:
            logger.info(
                "pi_message_received user_id=%s channel=%s requested_skill=%s text_chars=%d metadata_keys=%s",
                incoming.user_id,
                incoming.channel.value,
                incoming.requested_skill,
                len(incoming.text),
                sorted(incoming.metadata.keys()),
            )
            if incoming.context_id and _is_affirmative_confirmation(incoming.text):
                return await _confirm_pending_action(
                    request=ConfirmationRequest(
                        user_id=incoming.user_id,
                        channel=incoming.channel,
                        context_id=incoming.context_id,
                        skill=None,
                        confirmed=True,
                        metadata=incoming.metadata,
                    ),
                    directory=directory,
                    client=client,
                    store=store,
                    runtime=runtime if use_tool_loop else None,
                )
            if incoming.context_id and _is_negative_confirmation(incoming.text):
                try:
                    existing_context = store.get_context(incoming.context_id)
                except KeyError as exc:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Unknown context_id {incoming.context_id}.",
                    ) from exc
                pending_skill = (store.get_pending_confirmation(incoming.context_id) or {}).get("skill")
                store.clear_pending_confirmation(incoming.context_id)
                return _confirmation_not_sent_response(
                    context=existing_context,
                    request=ConfirmationRequest(
                        user_id=incoming.user_id,
                        channel=incoming.channel,
                        context_id=incoming.context_id,
                        skill=pending_skill,
                        confirmed=False,
                        metadata=incoming.metadata,
                    ),
                )
            user_message = UserMessage(
                channel=incoming.channel,
                sender_id=incoming.user_id,
                text=incoming.text,
            )
            context = _context_for_message(incoming, user_message, store)
            store.save_context(context)
            logger.debug(
                "pi_context_created context_id=%s message_id=%s",
                context.context_id,
                user_message.id,
            )

            if incoming.requested_skill and _looks_like_endpoint_url(incoming.requested_skill):
                logger.warning(
                    "pi_requested_skill_rejected_url context_id=%s requested_skill=%s",
                    context.context_id,
                    incoming.requested_skill,
                )
                raise HTTPException(
                    status_code=400,
                    detail="requested_skill must be a skill name, not an endpoint URL.",
                )

            if use_tool_loop and runtime is not None:
                logger.info(
                    "pi_tool_loop_start context_id=%s llm_model=%s",
                    context.context_id,
                    planner_config.model,
                )
                final = await runtime.run_turn(context=context, incoming=incoming)
                logger.info(
                    "pi_tool_loop_done context_id=%s state=%s workflow=%s confirmation_required=%s",
                    context.context_id,
                    final.state.value,
                    final.workflow,
                    final.confirmation_required,
                )
                return final.to_response()

            planning = await _plan_message(
                incoming=incoming,
                context=context,
                directory=directory,
                store=store,
                llm_config=planner_config,
                llm_planner=planner,
            )
            workflow_skills = planning["workflow"]
            workflow_inputs = planning.get("workflow_inputs", {})
            logger.info(
                "pi_workflow_planned context_id=%s planner_mode=%s llm_model=%s workflow=%s errors=%s",
                context.context_id,
                planning["planner_mode"],
                planning["llm_model"],
                workflow_skills,
                planning["errors"],
            )
            if not workflow_skills:
                logger.info(
                    "pi_workflow_empty context_id=%s planner_mode=%s",
                    context.context_id,
                    planning["planner_mode"],
                )
                return _planner_clarification_response(
                    context=context,
                    planning=planning,
                )
            results: list[A2ATaskResult] = []
            selected_steps: list[dict[str, str | None]] = []
            policy_decisions: list[dict[str, str]] = []
            artifacts: list[A2AArtifact] = []
            task_ids: dict[str, str] = {}
            task_states: dict[str, str] = {}
            structured_outputs: dict[str, object] = {}
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
                required_inputs = _safe_planner_inputs(workflow_inputs.get(skill, {}))
                try:
                    logger.info(
                        "pi_discovery_start context_id=%s skill=%s",
                        context.context_id,
                        skill,
                    )
                    routing = select_agent(
                        context_id=context.context_id,
                        user_goal=incoming.text,
                        directory_client=directory,
                        skill=skill,
                    )
                except LookupError as exc:
                    logger.warning(
                        "pi_discovery_no_agent context_id=%s skill=%s reason=%s",
                        context.context_id,
                        skill,
                        exc,
                    )
                    if incoming.requested_skill:
                        return _no_capable_agent_response(
                            context=context,
                            workflow_skills=workflow_skills,
                            requested_skill=incoming.requested_skill,
                            reason=str(exc),
                            planner_mode=str(planning["planner_mode"]),
                            llm_model=planning["llm_model"],
                            errors=planning["errors"],
                        )
                    raise
                selected_agent = routing.selected_agents[0]
                logger.info(
                    "pi_agent_selected context_id=%s skill=%s agent_id=%s agent_name=%s endpoint=%s source_record_id=%s",
                    context.context_id,
                    skill,
                    selected_agent.agent_id,
                    selected_agent.name,
                    selected_agent.endpoint,
                    selected_agent.source_record_id,
                )
                selected_capability = _capability_for_skill(selected_agent, skill)
                policy_metadata = _policy_metadata(incoming.metadata, required_inputs)
                policy_decision = evaluate_skill(skill, selected_capability, metadata=policy_metadata)
                policy_decisions.append(_policy_entry(skill, selected_agent, policy_decision))
                logger.info(
                    "pi_policy_decision context_id=%s skill=%s agent_id=%s status=%s confirmation_status=%s reason=%s",
                    context.context_id,
                    skill,
                    selected_agent.agent_id,
                    policy_decision.status.value,
                    policy_decision.confirmation_status.value,
                    policy_decision.reason,
                )
                if policy_decision.status.value == "denied":
                    raise HTTPException(status_code=403, detail=policy_decision.reason)
                if policy_decision.status.value == "requires_confirmation":
                    logger.info(
                        "pi_a2a_skipped_confirmation_required context_id=%s skill=%s agent_id=%s",
                        context.context_id,
                        skill,
                        selected_agent.agent_id,
                    )
                    pending_confirmation = _pending_confirmation(skill, selected_agent, policy_decision)
                    store.save_pending_confirmation(
                        context.context_id,
                        {
                            "skill": skill,
                            "agent_id": selected_agent.agent_id,
                            "required_inputs": required_inputs,
                            "policy_decision": policy_decision.model_dump(mode="json"),
                        },
                    )
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
                    required_inputs=required_inputs,
                )
                logger.info(
                    "pi_a2a_invoke_start context_id=%s skill=%s agent_id=%s endpoint=%s task_id=%s",
                    context.context_id,
                    skill,
                    selected_agent.agent_id,
                    selected_agent.endpoint,
                    task_request.task_id,
                )
                result = await client.send_task(selected_agent, task_request)
                logger.info(
                    "pi_a2a_invoke_done context_id=%s skill=%s agent_id=%s task_id=%s state=%s artifacts=%d structured_output=%s error=%s",
                    context.context_id,
                    skill,
                    selected_agent.agent_id,
                    result.task_id,
                    result.state.value,
                    len(result.artifacts),
                    result.structured_output is not None,
                    result.error,
                )
                results.append(result)
                artifacts.extend(result.artifacts)
                task_ids[skill] = result.task_id
                task_states[skill] = result.state.value
                if result.structured_output:
                    previous_outputs[skill] = result.structured_output
                    structured_outputs[skill] = result.structured_output
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
                store.save_pending_confirmation(
                    context.context_id,
                    {
                        "skill": EMAIL_SEND_SKILL,
                        "agent_id": email_agent,
                        "required_inputs": {},
                        "policy_decision": send_policy.model_dump(mode="json"),
                    },
                )

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
            fallback_response = _last_text(last_result) if last_result else ""
            final_response = await _synthesize_final_response(
                incoming=incoming,
                context=context,
                store=store,
                planning=planning,
                llm_config=planner_config,
                llm_planner=planner,
                workflow_skills=workflow_skills,
                selected_steps=selected_steps,
                structured_outputs=structured_outputs,
                source_artifacts=source_artifacts,
                fallback_text=fallback_response,
            )

            return {
                "context_id": context.context_id,
                "planner_mode": planning["planner_mode"],
                "llm_model": planning["llm_model"],
                "task_id": last_result.task_id if last_result else None,
                "task_ids": task_ids,
                "task_states": task_states,
                "state": last_result.state if last_result else A2ATaskState.input_required,
                "selected_agent": selected_steps[-1]["agent_id"] if selected_steps else None,
                "selected_agents": selected_steps,
                "workflow": workflow_skills,
                "llm_plan": planning["llm_plan"],
                "policy_decision": overall_policy,
                "policy_decisions": policy_decisions,
                "confirmation_status": overall_confirmation,
                "confirmation_required": pending_confirmation is not None,
                "pending_confirmation": pending_confirmation,
                "text": final_response,
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
                "structured_outputs": structured_outputs,
                "final_response": final_response,
                "errors": planning["errors"],
            }
        except HTTPException:
            raise
        except (PILLMConfigError, PILLMPlannerError) as exc:
            logger.exception("pi_llm_error")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("pi_message_failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/confirmations")
    async def post_confirmation(request: ConfirmationRequest) -> dict[str, object]:
        return await _confirm_pending_action(
            request=request,
            directory=directory,
            client=client,
            store=store,
            runtime=runtime if use_tool_loop else None,
        )

    return app


async def _confirm_pending_action(
    *,
    request: ConfirmationRequest,
    directory: DirectoryClient,
    client: A2AClient,
    store: InMemoryStateStore,
    runtime: PIAgentRuntime | None = None,
) -> dict[str, object]:
    logger.info(
        "pi_confirmation_received user_id=%s channel=%s context_id=%s skill=%s confirmed=%s",
        request.user_id,
        request.channel.value,
        request.context_id,
        request.skill,
        request.confirmed,
    )
    try:
        context = store.get_context(request.context_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown context_id {request.context_id}.") from exc

    pending = store.get_pending_confirmation(context.context_id) or {}
    skill = request.skill or pending.get("skill")
    if not isinstance(skill, str) or not skill:
        raise HTTPException(
            status_code=409,
            detail="No pending confirmation was found for this context.",
        )

    if not request.confirmed:
        store.clear_pending_confirmation(context.context_id)
        return _confirmation_not_sent_response(context=context, request=request)

    if runtime is not None and pending.get("runtime") == "tool_loop":
        try:
            final = await runtime.resume_confirmed_action(
                context=context,
                user_id=request.user_id,
                channel=request.channel,
                metadata=request.metadata,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=409,
                detail="No tool-loop pending action was found for this context.",
            ) from exc
        return final.to_response()

    invocations = store.list_invocations_for_context(context.context_id)
    previous_outputs, source_artifacts = _outputs_from_invocations(invocations)
    required_inputs = dict(pending.get("required_inputs") or {})
    draft_email = _draft_email_from_outputs(previous_outputs)
    if skill == EMAIL_SEND_SKILL:
        if not draft_email:
            raise HTTPException(
                status_code=409,
                detail="No prepared email draft was found for this context.",
            )
        required_inputs["draft_email"] = draft_email

    try:
        routing = select_agent(
            context_id=context.context_id,
            user_goal=context.current_goal,
            directory_client=directory,
            skill=skill,
        )
    except LookupError as exc:
        return _no_capable_agent_response(
            context=context,
            workflow_skills=[skill],
            requested_skill=skill,
            reason=str(exc),
        )
    selected_agent = routing.selected_agents[0]
    selected_capability = _capability_for_skill(selected_agent, skill)
    policy_metadata = _policy_metadata(request.metadata, _safe_planner_inputs(required_inputs))
    pending_policy = evaluate_skill(
        skill,
        selected_capability,
        metadata=policy_metadata,
    )
    if pending_policy.status != PolicyDecisionStatus.requires_confirmation:
        raise HTTPException(
            status_code=403,
            detail=f"Unexpected policy state for {skill}: {pending_policy.status.value}",
        )
    confirmed_policy = PolicyDecision(
        status=PolicyDecisionStatus.allowed,
        reason=f"User explicitly confirmed {skill} for this context.",
        confirmation_status=ConfirmationStatus.confirmed,
    )
    incoming = IncomingMessage(
        user_id=request.user_id,
        channel=request.channel,
        context_id=context.context_id,
        text=context.current_goal,
        requested_skill=skill,
        metadata=request.metadata,
    )
    task_request = _task_request(
        incoming=incoming,
        context=context,
        selected_agent=selected_agent,
        skill=skill,
        policy_decision=confirmed_policy,
        available_slots=None,
        previous_outputs=previous_outputs,
        source_artifacts=source_artifacts,
        reference_task_ids=[invocation.task_id for invocation in invocations],
        required_inputs=required_inputs,
        confirmed=True,
    )
    logger.info(
        "pi_confirmation_a2a_invoke_start context_id=%s skill=%s agent_id=%s endpoint=%s task_id=%s",
        context.context_id,
        skill,
        selected_agent.agent_id,
        selected_agent.endpoint,
        task_request.task_id,
    )
    result = await client.send_task(selected_agent, task_request)
    logger.info(
        "pi_confirmation_a2a_invoke_done context_id=%s skill=%s agent_id=%s task_id=%s state=%s",
        context.context_id,
        skill,
        selected_agent.agent_id,
        result.task_id,
        result.state.value,
    )
    store.save_invocation(
        AgentInvocationRecord(
            context_id=context.context_id,
            task_id=result.task_id,
            agent_id=selected_agent.agent_id,
            skill=skill,
            request_summary=context.current_goal,
            status=result.state,
            artifacts=result.artifacts,
            policy_decision=confirmed_policy,
            selected_agent=selected_agent,
            completed_at=result.messages[-1].created_at if result.messages else None,
            error=result.error,
        )
    )
    context.active_tasks.append(result.task_id)
    store.clear_pending_confirmation(context.context_id)
    store.save_context(context)

    return {
        "context_id": context.context_id,
        "task_id": result.task_id,
        "task_ids": {skill: result.task_id},
        "task_states": {skill: result.state.value},
        "state": result.state,
        "selected_agent": selected_agent.agent_id,
        "selected_agents": [
            {
                "skill": skill,
                "agent_id": selected_agent.agent_id,
                "task_id": result.task_id,
            }
        ],
        "workflow": [skill],
        "policy_decision": confirmed_policy.status.value,
        "policy_decisions": [
            _policy_entry(skill, selected_agent, pending_policy),
            _policy_entry(skill, selected_agent, confirmed_policy),
        ],
        "confirmation_status": confirmed_policy.confirmation_status.value,
        "confirmation_required": False,
        "pending_confirmation": None,
        "text": _last_text(result),
        "draft_email": draft_email,
        "source_artifacts": source_artifacts,
        "artifacts": [artifact.model_dump(mode="json") for artifact in result.artifacts],
        "structured_output": result.structured_output,
        "structured_outputs": {skill: result.structured_output},
        "final_response": _last_text(result),
    }


async def _plan_message(
    *,
    incoming: IncomingMessage,
    context: PIContext,
    directory: DirectoryClient,
    store: InMemoryStateStore,
    llm_config: PILLMConfig,
    llm_planner: PILLMPlanner | None,
) -> dict[str, Any]:
    if not llm_config.enabled:
        logger.debug(
            "pi_deterministic_planner_start context_id=%s requested_skill=%s",
            context.context_id,
            incoming.requested_skill,
        )
        return {
            "workflow": plan_workflow(incoming.text, requested_skill=incoming.requested_skill),
            "workflow_inputs": {},
            "planner_mode": "deterministic",
            "llm_model": None,
            "llm_plan": None,
            "errors": [],
        }
    if llm_planner is None:
        raise PILLMPlannerError("PI_LLM_ENABLED=true but no LLM planner is configured.")
    try:
        logger.info(
            "pi_llm_catalog_start context_id=%s requested_skill=%s",
            context.context_id,
            incoming.requested_skill,
        )
        available_skills = build_skill_catalog(
            directory,
            skill_hints=[incoming.requested_skill] if incoming.requested_skill else None,
        )
        available_skill_ids = {item.skill for item in available_skills}
        logger.info(
            "pi_llm_catalog_done context_id=%s available_skill_count=%d available_skills=%s",
            context.context_id,
            len(available_skills),
            sorted(available_skill_ids),
        )
        if incoming.requested_skill and incoming.requested_skill not in available_skill_ids:
            logger.warning(
                "pi_requested_skill_not_discovered context_id=%s requested_skill=%s",
                context.context_id,
                incoming.requested_skill,
            )
            return {
                "workflow": [incoming.requested_skill],
                "workflow_inputs": {},
                "planner_mode": "llm",
                "llm_model": llm_config.model,
                "llm_plan": {
                    "workflow": [],
                    "needs_clarification": True,
                    "clarifying_question": (
                        f"No capable agent found for requested_skill {incoming.requested_skill}."
                    ),
                    "user_visible_plan": "No discovered agent can satisfy the requested skill.",
                },
                "errors": [],
            }
        plan = await llm_planner.plan(
            user_text=incoming.text,
            requested_skill=incoming.requested_skill,
            available_skills=available_skills,
            conversation_context=_planner_conversation_context(context, store),
        )
        logger.info(
            "pi_llm_plan_done context_id=%s workflow=%s needs_clarification=%s",
            context.context_id,
            [step.skill for step in plan.workflow],
            plan.needs_clarification,
        )
        workflow, workflow_inputs = _normalize_llm_workflow(
            workflow=[step.skill for step in plan.workflow],
            workflow_inputs=_workflow_inputs_from_plan(plan),
            requested_skill=incoming.requested_skill,
            available_skill_ids=available_skill_ids,
        )
        return {
            "workflow": workflow,
            "workflow_inputs": workflow_inputs,
            "planner_mode": "llm",
            "llm_model": llm_config.model,
            "llm_plan": plan.model_dump(mode="json"),
            "errors": [],
        }
    except PILLMPlannerError as exc:
        if not llm_config.allow_deterministic_fallback:
            raise
        logger.warning(
            "pi_llm_fallback_to_deterministic context_id=%s error=%s",
            context.context_id,
            exc,
        )
        return {
            "workflow": plan_workflow(incoming.text, requested_skill=incoming.requested_skill),
            "workflow_inputs": {},
            "planner_mode": "deterministic_fallback",
            "llm_model": llm_config.model,
            "llm_plan": None,
            "errors": [str(exc)],
        }


def _planner_clarification_response(
    *,
    context: PIContext,
    planning: dict[str, Any],
) -> dict[str, object]:
    plan = planning.get("llm_plan") or {}
    text = (
        plan.get("clarifying_question")
        or plan.get("user_visible_plan")
        or "No executable workflow was produced by the PI planner."
    )
    return {
        "context_id": context.context_id,
        "planner_mode": planning["planner_mode"],
        "llm_model": planning["llm_model"],
        "task_id": None,
        "task_ids": {},
        "task_states": {},
        "state": A2ATaskState.input_required,
        "selected_agent": None,
        "selected_agents": [],
        "workflow": [],
        "llm_plan": plan or None,
        "policy_decision": "denied",
        "policy_decisions": [],
        "confirmation_status": "not_required",
        "confirmation_required": False,
        "pending_confirmation": None,
        "text": text,
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
        "structured_outputs": {},
        "final_response": text,
        "errors": planning["errors"],
    }


def _context_for_message(
    incoming: IncomingMessage,
    user_message: UserMessage,
    store: InMemoryStateStore,
) -> PIContext:
    if incoming.context_id:
        try:
            context = store.get_context(incoming.context_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown context_id {incoming.context_id}.",
            ) from exc
        context.current_goal = incoming.text
        context.updated_at = utc_now()
        context.conversation_history.append(incoming.text)
        return context
    return PIContext(
        context_id=str(uuid4()),
        user_id=incoming.user_id,
        channel=incoming.channel,
        original_message_id=user_message.id,
        current_goal=incoming.text,
        conversation_history=[incoming.text],
    )


def _planner_conversation_context(
    context: PIContext,
    store: InMemoryStateStore,
) -> dict[str, Any]:
    invocations = store.list_invocations_for_context(context.context_id)
    previous_outputs, source_artifacts = _outputs_from_invocations(invocations)
    return {
        "context_id": context.context_id,
        "user_id": context.user_id,
        "channel": context.channel.value,
        "current_goal": context.current_goal,
        "conversation_history": context.conversation_history[-12:],
        "active_tasks": context.active_tasks[-12:],
        "pending_confirmation": store.get_pending_confirmation(context.context_id),
        "recent_invocations": [
            {
                "skill": invocation.skill,
                "task_id": invocation.task_id,
                "agent_id": invocation.agent_id,
                "status": invocation.status.value,
                "policy_decision": invocation.policy_decision.status.value,
                "confirmation_status": invocation.policy_decision.confirmation_status.value,
                "selected_agent": invocation.selected_agent.name,
                "endpoint": invocation.selected_agent.endpoint,
            }
            for invocation in invocations[-12:]
        ],
        "previous_outputs": previous_outputs,
        "source_artifacts": source_artifacts,
    }


async def _synthesize_final_response(
    *,
    incoming: IncomingMessage,
    context: PIContext,
    store: InMemoryStateStore,
    planning: dict[str, Any],
    llm_config: PILLMConfig,
    llm_planner: PILLMPlanner | None,
    workflow_skills: list[str],
    selected_steps: list[dict[str, str | None]],
    structured_outputs: dict[str, object],
    source_artifacts: list[dict[str, str]],
    fallback_text: str,
) -> str:
    if (
        not llm_config.enabled
        or llm_planner is None
        or not structured_outputs
        or planning["planner_mode"] not in {"llm", "deterministic_fallback"}
    ):
        return fallback_text
    try:
        return await llm_planner.synthesize_response(
            user_text=incoming.text,
            conversation_context=_planner_conversation_context(context, store),
            workflow=workflow_skills,
            selected_agents=[dict(step) for step in selected_steps],
            structured_outputs=structured_outputs,
            source_artifacts=[dict(item) for item in source_artifacts],
            fallback_text=fallback_text,
        )
    except PILLMPlannerError as exc:
        planning["errors"].append(str(exc))
        logger.warning(
            "pi_llm_synthesis_fallback context_id=%s error=%s",
            context.context_id,
            exc,
        )
        return fallback_text


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


def _workflow_inputs_from_plan(plan: Any) -> dict[str, dict[str, Any]]:
    inputs: dict[str, dict[str, Any]] = {}
    for step in getattr(plan, "workflow", []):
        cleaned = _safe_planner_inputs(getattr(step, "required_inputs", {}))
        if cleaned:
            inputs[step.skill] = cleaned
    return inputs


def _normalize_llm_workflow(
    *,
    workflow: list[str],
    workflow_inputs: dict[str, dict[str, Any]],
    requested_skill: str | None,
    available_skill_ids: set[str],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    normalized_workflow = list(workflow)
    normalized_inputs = {skill: dict(inputs) for skill, inputs in workflow_inputs.items()}
    if requested_skill:
        return normalized_workflow, normalized_inputs
    if EMAIL_SEND_SKILL not in normalized_workflow:
        return normalized_workflow, normalized_inputs
    if EMAIL_DRAFT_SKILL not in available_skill_ids:
        return normalized_workflow, normalized_inputs

    send_index = normalized_workflow.index(EMAIL_SEND_SKILL)
    if EMAIL_DRAFT_SKILL in normalized_workflow[:send_index]:
        return normalized_workflow, normalized_inputs

    draft_inputs = dict(
        normalized_inputs.get(EMAIL_DRAFT_SKILL)
        or normalized_inputs.get(EMAIL_SEND_SKILL)
        or {}
    )
    draft_inputs.setdefault("create_gmail_draft", False)
    normalized_inputs[EMAIL_DRAFT_SKILL] = draft_inputs
    normalized_workflow.insert(send_index, EMAIL_DRAFT_SKILL)
    logger.info(
        "pi_llm_workflow_normalized inserted=%s before=%s workflow=%s",
        EMAIL_DRAFT_SKILL,
        EMAIL_SEND_SKILL,
        normalized_workflow,
    )
    return normalized_workflow, normalized_inputs


def _policy_metadata(user_metadata: dict[str, Any], planner_inputs: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(user_metadata)
    metadata.update(planner_inputs)
    return metadata


_PLANNER_INPUT_KEYS = {
    "to",
    "cc",
    "bcc",
    "recipient_name",
    "subject",
    "body",
    "create_gmail_draft",
}


def _safe_planner_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(inputs, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in inputs.items():
        if key not in _PLANNER_INPUT_KEYS:
            continue
        if value is None or value == "":
            continue
        if isinstance(value, list):
            list_value = [
                item.strip()
                for item in value
                if isinstance(item, str) and item.strip() and not _contains_raw_endpoint(item)
            ]
            if list_value:
                cleaned[key] = list_value
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped and not _contains_raw_endpoint(stripped):
                cleaned[key] = stripped
            continue
        if isinstance(value, bool):
            cleaned[key] = value
    return cleaned


def _contains_raw_endpoint(value: str) -> bool:
    lowered = value.strip().lower()
    return "://" in lowered or lowered.startswith(("localhost:", "127.", "0.0.0.0", "[::1]"))


def _is_affirmative_confirmation(text: str) -> bool:
    normalized = _normalize_confirmation_text(text)
    return normalized in {
        "yes",
        "y",
        "confirm",
        "confirmed",
        "send",
        "send it",
        "go ahead",
        "looks good",
        "ok send",
        "okay send",
        "yes send",
    }


def _is_negative_confirmation(text: str) -> bool:
    normalized = _normalize_confirmation_text(text)
    return normalized in {
        "no",
        "n",
        "cancel",
        "do not send",
        "dont send",
        "don't send",
        "stop",
    }


def _normalize_confirmation_text(text: str) -> str:
    return " ".join(text.strip().lower().replace(".", "").replace("!", "").split())


def _no_capable_agent_response(
    *,
    context: PIContext,
    workflow_skills: list[str],
    requested_skill: str,
    reason: str,
    planner_mode: str = "deterministic",
    llm_model: object | None = None,
    errors: list[str] | None = None,
) -> dict[str, object]:
    return {
        "context_id": context.context_id,
        "planner_mode": planner_mode,
        "llm_model": llm_model,
        "task_id": None,
        "task_ids": {},
        "task_states": {},
        "state": A2ATaskState.input_required,
        "selected_agent": None,
        "selected_agents": [],
        "workflow": workflow_skills,
        "llm_plan": None,
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
        "structured_outputs": {},
        "final_response": f"no capable agent found for requested_skill {requested_skill}",
        "errors": errors or [],
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
    required_inputs: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> A2ATaskRequest:
    parts = [A2APart(kind="text", text=incoming.text)]
    metadata = {
        "user_id": incoming.user_id,
        "channel": incoming.channel.value,
        "policy_decision": policy_decision.status.value,
        "confirmation_status": policy_decision.confirmation_status.value,
    }
    task_metadata = _safe_user_metadata(incoming.metadata)
    task_metadata.update(_safe_planner_inputs(required_inputs or {}))
    if skill == "email/draft":
        task_metadata.setdefault("create_gmail_draft", False)
    metadata.update(task_metadata)
    if confirmed:
        metadata["confirmed"] = True
        metadata["confirmation_status"] = ConfirmationStatus.confirmed.value
    if task_metadata:
        parts.append(A2APart(kind="data", data=dict(task_metadata)))
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


def _outputs_from_invocations(
    invocations: list[AgentInvocationRecord],
) -> tuple[dict[str, object], list[dict[str, str]]]:
    outputs: dict[str, object] = {}
    source_artifacts: list[dict[str, str]] = []
    for invocation in invocations:
        for artifact in invocation.artifacts:
            source_artifacts.append(
                {
                    "skill": invocation.skill,
                    "task_id": invocation.task_id,
                    "artifact_id": artifact.artifact_id,
                    "name": artifact.name,
                }
            )
            for part in artifact.parts:
                if part.kind == "data" and isinstance(part.data, dict):
                    outputs[invocation.skill] = part.data
                    break
    return outputs, source_artifacts


def _draft_email_from_outputs(previous_outputs: dict[str, object]) -> dict[str, object] | None:
    draft_output = previous_outputs.get("email/draft")
    if isinstance(draft_output, dict) and isinstance(draft_output.get("draft_email"), dict):
        return draft_output["draft_email"]
    for output in previous_outputs.values():
        if isinstance(output, dict) and isinstance(output.get("draft_email"), dict):
            return output["draft_email"]
    return None


def _confirmation_not_sent_response(
    *,
    context: PIContext,
    request: ConfirmationRequest,
) -> dict[str, object]:
    skill = request.skill or "pending_action"
    return {
        "context_id": context.context_id,
        "task_id": None,
        "task_ids": {},
        "task_states": {},
        "state": A2ATaskState.input_required,
        "selected_agent": None,
        "selected_agents": [],
        "workflow": [skill],
        "policy_decision": "requires_confirmation",
        "policy_decisions": [
            {
                "skill": skill,
                "agent_id": None,
                "policy_decision": "requires_confirmation",
                "confirmation_status": ConfirmationStatus.rejected.value,
            }
        ],
        "confirmation_status": ConfirmationStatus.rejected.value,
        "confirmation_required": True,
        "pending_confirmation": {
            "skill": skill,
            "agent_id": None,
            "policy_decision": "requires_confirmation",
            "confirmation_status": ConfirmationStatus.pending.value,
            "reason": "User did not confirm the side-effecting action.",
        },
        "text": "Email was not sent because confirmation was not granted.",
        "source_artifacts": [],
        "artifacts": [],
        "structured_output": None,
        "structured_outputs": {},
        "final_response": "Email was not sent because confirmation was not granted.",
    }


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


_CHAT_UI_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PI Agent Chat</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #f5f7fb; color: #172033; }
    main { max-width: 860px; margin: 0 auto; min-height: 100vh; display: grid; grid-template-rows: auto 1fr auto; }
    header { padding: 22px 18px 14px; border-bottom: 1px solid #d9deea; }
    h1 { font-size: 20px; margin: 0 0 6px; font-weight: 650; }
    .status { font-size: 13px; color: #5c687d; }
    #log { padding: 18px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }
    .msg { max-width: 82%; padding: 12px 14px; border-radius: 8px; line-height: 1.42; white-space: pre-wrap; box-shadow: 0 1px 2px rgba(20, 32, 52, 0.06); }
    .user { align-self: flex-end; background: #1f6feb; color: white; }
    .agent { align-self: flex-start; background: white; border: 1px solid #dfe5ef; }
    .meta { display: block; margin-top: 8px; color: #667085; font-size: 12px; white-space: normal; }
    form { display: grid; grid-template-columns: 1fr auto; gap: 10px; padding: 14px 18px 18px; border-top: 1px solid #d9deea; background: #fff; }
    textarea { resize: none; min-height: 46px; max-height: 140px; padding: 12px; border: 1px solid #c8d0df; border-radius: 8px; font: inherit; }
    button { border: 0; border-radius: 8px; background: #0f766e; color: white; padding: 0 18px; font: inherit; font-weight: 650; cursor: pointer; }
    button:disabled { background: #9aa4b2; cursor: wait; }
    code { background: #eef2f7; border-radius: 4px; padding: 1px 4px; }
  </style>
</head>
<body>
<main>
  <header>
    <h1>PI Agent Chat</h1>
    <div class="status" id="status">Ready. Ask PI to use discovered A2A agents.</div>
  </header>
  <section id="log"></section>
  <form id="form">
    <textarea id="input" placeholder="Ask PI Agent..." autofocus></textarea>
    <button id="send" type="submit">Send</button>
  </form>
</main>
<script>
let contextId = null;
const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("input");
const button = document.getElementById("send");
const statusLine = document.getElementById("status");

function addMessage(role, text, meta) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text || "";
  if (meta) {
    const span = document.createElement("span");
    span.className = "meta";
    span.textContent = meta;
    div.appendChild(span);
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function responseText(data) {
  if (data.structured_output && data.structured_output.sent_email) {
    const sent = data.structured_output.sent_email;
    return "Sent.\\nTo: " + (sent.to || "") + "\\nSubject: " + (sent.subject || "");
  }
  if (data.draft_email) {
    const draft = data.draft_email;
    return [
      "Draft ready:",
      "To: " + (draft.to || ""),
      "Subject: " + (draft.subject || ""),
      "",
      draft.body || "",
      "",
      data.confirmation_required ? "Type yes to send, or no to cancel." : ""
    ].join("\\n").trim();
  }
  if (data.final_response) return data.final_response;
  if (data.text) return data.text;
  if (data.pending_confirmation) return "Confirmation required. Type yes to proceed or no to cancel.";
  return JSON.stringify(data, null, 2);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addMessage("user", text);
  button.disabled = true;
  statusLine.textContent = "Working...";
  try {
    const response = await fetch("/messages", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        user_id: "web-user",
        channel: "api",
        text,
        context_id: contextId
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || response.statusText);
    contextId = data.context_id || contextId;
    addMessage("agent", responseText(data), contextId ? "context_id: " + contextId : "");
    statusLine.textContent = data.confirmation_required ? "Waiting for confirmation." : "Ready.";
  } catch (error) {
    addMessage("agent", "Error: " + error.message);
    statusLine.textContent = "Error.";
  } finally {
    button.disabled = false;
    input.focus();
  }
});
</script>
</body>
</html>
"""


app = create_app(
    publish_weather_card=os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true",
    publish_calendar_email_cards=os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true",
    publish_geo_intelligence_cards=os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true",
    publish_coding_card=os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true",
)
