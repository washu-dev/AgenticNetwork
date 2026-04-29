from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.common.models import (
    A2AArtifact,
    A2AMessage,
    A2APart,
    A2ATaskRequest,
    A2ATaskResult,
    A2ATaskState,
    AgentCapability,
    AgentInvocationRecord,
    ConfirmationStatus,
    DiscoveredAgent,
    PIContext,
    PolicyDecision,
    PolicyDecisionStatus,
)
from v2_agent_network.services.directory.client import DirectoryClient, DirectoryUnavailable
from v2_agent_network.services.pi_agent.llm_config import PILLMConfig
from v2_agent_network.services.pi_agent.llm_planner import PILLMPlannerError
from v2_agent_network.services.pi_agent.policy import evaluate_skill
from v2_agent_network.services.state.store import InMemoryStateStore

logger = logging.getLogger(__name__)


class AgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None


class AgentObservation(BaseModel):
    tool_name: str
    status: str
    content: dict[str, Any] = Field(default_factory=dict)


class AgentTurnState(BaseModel):
    context_id: str
    user_text: str
    requested_skill: str | None = None
    step: int = 0
    ads_skill_catalog: list[dict[str, Any]] = Field(default_factory=list)
    ads_catalog_error: str | None = None
    discovered_agents: dict[str, DiscoveredAgent] = Field(default_factory=dict)
    observations: list[AgentObservation] = Field(default_factory=list)
    selected_agents: list[dict[str, str | None]] = Field(default_factory=list)
    policy_decisions: list[dict[str, str]] = Field(default_factory=list)
    artifacts: list[A2AArtifact] = Field(default_factory=list)
    source_artifacts: list[dict[str, str]] = Field(default_factory=list)
    structured_outputs: dict[str, object] = Field(default_factory=dict)
    task_ids: dict[str, str] = Field(default_factory=dict)
    task_states: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class AgentFinal(BaseModel):
    context_id: str
    state: A2ATaskState
    text: str
    planner_mode: str = "tool_loop"
    llm_model: str | None = None
    selected_agents: list[dict[str, str | None]] = Field(default_factory=list)
    workflow: list[str] = Field(default_factory=list)
    task_ids: dict[str, str] = Field(default_factory=dict)
    task_states: dict[str, str] = Field(default_factory=dict)
    policy_decisions: list[dict[str, str]] = Field(default_factory=list)
    confirmation_required: bool = False
    pending_confirmation: dict[str, Any] | None = None
    artifacts: list[A2AArtifact] = Field(default_factory=list)
    source_artifacts: list[dict[str, str]] = Field(default_factory=list)
    structured_outputs: dict[str, object] = Field(default_factory=dict)
    structured_output: dict[str, Any] | None = None
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def to_response(self) -> dict[str, object]:
        policy_decision = (
            self.pending_confirmation["policy_decision"]
            if self.pending_confirmation
            else (
                self.policy_decisions[-1]["policy_decision"]
                if self.policy_decisions
                else "allowed"
            )
        )
        confirmation_status = (
            self.pending_confirmation["confirmation_status"]
            if self.pending_confirmation
            else (
                self.policy_decisions[-1]["confirmation_status"]
                if self.policy_decisions
                else ConfirmationStatus.not_required.value
            )
        )
        return {
            "context_id": self.context_id,
            "planner_mode": self.planner_mode,
            "llm_model": self.llm_model,
            "task_id": next(reversed(self.task_ids.values()), None),
            "task_ids": self.task_ids,
            "task_states": self.task_states,
            "state": self.state,
            "selected_agent": (
                self.selected_agents[-1]["agent_id"] if self.selected_agents else None
            ),
            "selected_agents": self.selected_agents,
            "workflow": self.workflow,
            "llm_plan": None,
            "policy_decision": policy_decision,
            "policy_decisions": self.policy_decisions,
            "confirmation_status": confirmation_status,
            "confirmation_required": self.confirmation_required,
            "pending_confirmation": self.pending_confirmation,
            "text": self.text,
            "available_slots": [],
            "draft_email": _draft_email_from_outputs(self.structured_outputs),
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
            "source_artifacts": self.source_artifacts,
            "artifacts": [artifact.model_dump(mode="json") for artifact in self.artifacts],
            "structured_output": self.structured_output,
            "structured_outputs": self.structured_outputs,
            "tool_trace": self.tool_trace,
            "final_response": self.text,
            "errors": self.errors,
        }


class ToolLoopLLMClient(Protocol):
    async def next_tool_call(self, state: AgentTurnState, tools: list[dict[str, Any]]) -> AgentToolCall:
        ...


class OpenAIToolLoopClient:
    def __init__(self, config: PILLMConfig, openai_client: Any | None = None):
        self.config = config
        self._openai_client = openai_client
        self._previous_response_id: str | None = None
        self._last_call_id: str | None = None

    async def next_tool_call(self, state: AgentTurnState, tools: list[dict[str, Any]]) -> AgentToolCall:
        client = self._openai_client or _default_openai_client(self.config)
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "tools": tools,
            "tool_choice": "auto",
            "max_output_tokens": self.config.max_output_tokens,
        }
        if self.config.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.config.reasoning_effort}
        if self.config.verbosity:
            kwargs["text"] = {"verbosity": self.config.verbosity}
        if self._previous_response_id and self._last_call_id and state.observations:
            kwargs["previous_response_id"] = self._previous_response_id
            kwargs["input"] = [
                {
                    "type": "function_call_output",
                    "call_id": self._last_call_id,
                    "output": json.dumps(
                        state.observations[-1].model_dump(mode="json"),
                        sort_keys=True,
                        default=str,
                    ),
                }
            ]
        else:
            kwargs["input"] = [
                {"role": "system", "content": _tool_loop_system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(_initial_llm_payload(state), sort_keys=True, default=str),
                },
            ]
        try:
            response = await client.responses.create(**kwargs)
        except Exception as exc:
            logger.exception("pi_tool_loop_openai_request_failed model=%s", self.config.model)
            raise PILLMPlannerError(f"OpenAI tool-loop request failed: {type(exc).__name__}: {exc}") from exc

        self._previous_response_id = getattr(response, "id", None) or (
            response.get("id") if isinstance(response, dict) else None
        )
        tool_call = _tool_call_from_response(response)
        if tool_call.call_id:
            self._last_call_id = tool_call.call_id
        return tool_call


class PIAgentRuntime:
    def __init__(
        self,
        *,
        directory_client: DirectoryClient,
        a2a_client: A2AClient,
        state_store: InMemoryStateStore,
        llm_config: PILLMConfig,
        llm_client: ToolLoopLLMClient | None = None,
        max_steps: int | None = None,
    ):
        self.directory = directory_client
        self.a2a_client = a2a_client
        self.store = state_store
        self.llm_config = llm_config
        self.llm_client = llm_client or OpenAIToolLoopClient(llm_config)
        self.max_steps = max_steps or int(os.getenv("PI_AGENT_MAX_STEPS", "8"))

    async def run_turn(self, *, context: PIContext, incoming: Any) -> AgentFinal:
        state = AgentTurnState(
            context_id=context.context_id,
            user_text=incoming.text,
            requested_skill=getattr(incoming, "requested_skill", None),
        )
        if state.requested_skill and _looks_like_endpoint_url(state.requested_skill):
            raise ValueError("requested_skill must be a skill name, not an endpoint URL.")
        self._load_ads_skill_catalog(state)

        for step in range(self.max_steps):
            state.step = step
            tool_call = await self.llm_client.next_tool_call(state, _tool_schemas())
            logger.info(
                "pi_tool_loop_call context_id=%s step=%d tool=%s",
                context.context_id,
                step,
                tool_call.tool_name,
            )
            final = await self._execute_tool(tool_call, state=state, context=context, incoming=incoming)
            if final is not None:
                return final

        return self._final(
            state,
            text="I reached the maximum number of tool steps before finishing.",
            a2a_state=A2ATaskState.input_required,
            errors=["max_steps_exceeded"],
        )

    def _load_ads_skill_catalog(self, state: AgentTurnState) -> None:
        try:
            agents = self.directory.search_agents()
        except Exception as exc:
            state.ads_catalog_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "pi_tool_loop_ads_catalog_failed context_id=%s error=%s",
                state.context_id,
                state.ads_catalog_error,
            )
            return
        state.ads_skill_catalog = _ads_skill_catalog(agents)
        logger.info(
            "pi_tool_loop_ads_catalog_loaded context_id=%s skills=%d agents=%s",
            state.context_id,
            len(state.ads_skill_catalog),
            sorted({item["agent_id"] for item in state.ads_skill_catalog}),
        )

    async def resume_confirmed_action(
        self,
        *,
        context: PIContext,
        user_id: str,
        channel: Any,
        metadata: dict[str, Any] | None = None,
    ) -> AgentFinal:
        pending = self.store.get_pending_confirmation(context.context_id) or {}
        if pending.get("runtime") != "tool_loop":
            raise KeyError("No tool-loop pending action was found for this context.")
        agent = DiscoveredAgent.model_validate(pending["selected_agent"])
        skill = str(pending["skill"])
        task_input = dict(pending.get("task_input") or {})
        previous_outputs = dict(pending.get("previous_outputs") or {})
        source_artifacts = [dict(item) for item in pending.get("source_artifacts") or []]
        confirmed_policy = PolicyDecision(
            status=PolicyDecisionStatus.allowed,
            reason=f"User explicitly confirmed {skill} for this context.",
            confirmation_status=ConfirmationStatus.confirmed,
        )
        request = self._task_request(
            context=context,
            user_text=context.current_goal,
            user_id=user_id,
            channel_value=getattr(channel, "value", str(channel)),
            agent=agent,
            skill=skill,
            task_input=task_input,
            policy_decision=confirmed_policy,
            previous_outputs=previous_outputs,
            source_artifacts=source_artifacts,
            reference_task_ids=list(context.active_tasks),
            confirmed=True,
            metadata=metadata or {},
        )
        try:
            result = await self.a2a_client.send_task(agent, request)
        except Exception as exc:
            logger.exception(
                "pi_tool_loop_confirmed_a2a_failed context_id=%s skill=%s agent_id=%s endpoint=%s task_id=%s",
                context.context_id,
                skill,
                agent.agent_id,
                agent.endpoint,
                request.task_id,
            )
            result = _failed_agent_result(request=request, agent=agent, exc=exc)
        self._save_invocation(
            context=context,
            agent=agent,
            skill=skill,
            result=result,
            policy_decision=confirmed_policy,
            request_summary=context.current_goal,
        )
        self.store.clear_pending_confirmation(context.context_id)
        final = AgentFinal(
            context_id=context.context_id,
            state=result.state,
            text=_last_text(result) or "Done.",
            llm_model=self.llm_config.model,
            selected_agents=[{"skill": skill, "agent_id": agent.agent_id, "task_id": result.task_id}],
            workflow=[skill],
            task_ids={skill: result.task_id},
            task_states={skill: result.state.value},
            policy_decisions=[
                {
                    "skill": skill,
                    "agent_id": agent.agent_id,
                    "policy_decision": PolicyDecisionStatus.requires_confirmation.value,
                    "confirmation_status": ConfirmationStatus.pending.value,
                },
                {
                    "skill": skill,
                    "agent_id": agent.agent_id,
                    "policy_decision": confirmed_policy.status.value,
                    "confirmation_status": confirmed_policy.confirmation_status.value,
                },
            ],
            artifacts=result.artifacts,
            source_artifacts=[*source_artifacts, *_artifact_references(result, skill)],
            structured_outputs={skill: result.structured_output} if result.structured_output else {},
            structured_output=result.structured_output,
            tool_trace=[
                {
                    "tool_name": "call_agent",
                    "status": result.state.value,
                    "skill": skill,
                    "agent_id": agent.agent_id,
                    "confirmed": True,
                }
            ],
        )
        return final

    async def _execute_tool(
        self,
        tool_call: AgentToolCall,
        *,
        state: AgentTurnState,
        context: PIContext,
        incoming: Any,
    ) -> AgentFinal | None:
        name = tool_call.tool_name
        args = dict(tool_call.arguments or {})
        if name == "read_context":
            observation = AgentObservation(
                tool_name=name,
                status="ok",
                content=_context_snapshot(context, self.store),
            )
        elif name == "discover_agents":
            observation = self._discover_agents(args, state=state)
        elif name == "inspect_agent":
            observation = self._inspect_agent(args, state=state)
        elif name == "call_agent":
            maybe_final = await self._call_agent(args, state=state, context=context, incoming=incoming)
            if isinstance(maybe_final, AgentFinal):
                return maybe_final
            observation = maybe_final
        elif name == "request_confirmation":
            return self._request_confirmation(args, state=state, context=context)
        elif name == "ask_user":
            return self._final(state, text=str(args.get("text") or "I need more information."), a2a_state=A2ATaskState.input_required)
        elif name == "final_answer":
            return self._final(state, text=str(args.get("text") or ""), a2a_state=A2ATaskState.completed)
        else:
            observation = AgentObservation(
                tool_name=name,
                status="error",
                content={"error": f"Unknown tool {name}."},
            )
        state.observations.append(observation)
        return None

    def _discover_agents(self, args: dict[str, Any], *, state: AgentTurnState) -> AgentObservation:
        skill_hint = args.get("skill_hint") or state.requested_skill
        if isinstance(skill_hint, str) and _looks_like_endpoint_url(skill_hint):
            return AgentObservation(
                tool_name="discover_agents",
                status="error",
                content={"error": "skill_hint must be a skill name, not an endpoint URL."},
            )
        try:
            agents = self.directory.search_agents(skill=skill_hint if isinstance(skill_hint, str) else None)
        except DirectoryUnavailable as exc:
            state.errors.append(str(exc))
            return AgentObservation(
                tool_name="discover_agents",
                status="error",
                content={
                    "query": args.get("query"),
                    "skill_hint": skill_hint,
                    "error": str(exc),
                    "hint": (
                        "The requested capability was not discoverable in routed ADS. "
                        "Use discover_agents with skill_hint=null to inspect currently routed agents, "
                        "or ask the ADS host to route/republish the missing agent."
                    ),
                },
            )
        for agent in agents:
            state.discovered_agents[agent.agent_id] = agent
        return AgentObservation(
            tool_name="discover_agents",
            status="ok",
            content={
                "query": args.get("query"),
                "skill_hint": skill_hint,
                "agents": [_safe_agent(agent) for agent in agents],
            },
        )

    def _inspect_agent(self, args: dict[str, Any], *, state: AgentTurnState) -> AgentObservation:
        agent_id = str(args.get("agent_id") or "")
        agent = state.discovered_agents.get(agent_id)
        if agent is None:
            return AgentObservation(
                tool_name="inspect_agent",
                status="error",
                content={"error": f"Agent {agent_id!r} has not been discovered in this turn."},
            )
        return AgentObservation(tool_name="inspect_agent", status="ok", content=_safe_agent(agent, include_details=True))

    async def _call_agent(
        self,
        args: dict[str, Any],
        *,
        state: AgentTurnState,
        context: PIContext,
        incoming: Any,
    ) -> AgentObservation | AgentFinal:
        agent_id = str(args.get("agent_id") or "")
        skill = str(args.get("skill") or "")
        task_input = args.get("task_input") if isinstance(args.get("task_input"), dict) else {}
        if _looks_like_endpoint_url(agent_id) or _looks_like_endpoint_url(skill):
            return AgentObservation(
                tool_name="call_agent",
                status="error",
                content={"error": "call_agent accepts discovered agent_id and skill only, not URLs."},
            )
        agent = state.discovered_agents.get(agent_id)
        if agent is None:
            return AgentObservation(
                tool_name="call_agent",
                status="error",
                content={"error": f"Agent {agent_id!r} has not been discovered in this turn."},
            )
        capability = _capability_for_skill(agent, skill)
        policy_metadata = dict(task_input)
        policy_decision = evaluate_skill(skill, capability, metadata=policy_metadata)
        policy_entry = _policy_entry(skill, agent, policy_decision)
        state.policy_decisions.append(policy_entry)
        if policy_decision.status == PolicyDecisionStatus.denied:
            return AgentObservation(
                tool_name="call_agent",
                status="denied",
                content={"skill": skill, "agent_id": agent_id, "reason": policy_decision.reason},
            )
        previous_outputs, source_artifacts = _outputs_from_invocations(
            self.store.list_invocations_for_context(context.context_id)
        )
        if policy_decision.status == PolicyDecisionStatus.requires_confirmation:
            pending = _pending_confirmation(skill, agent, policy_decision)
            pending.update(
                {
                    "runtime": "tool_loop",
                    "selected_agent": agent.model_dump(mode="json"),
                    "task_input": task_input,
                    "previous_outputs": previous_outputs,
                    "source_artifacts": source_artifacts,
                }
            )
            self.store.save_pending_confirmation(context.context_id, pending)
            state.selected_agents.append({"skill": skill, "agent_id": agent.agent_id, "task_id": None})
            return self._final(
                state,
                text=policy_decision.reason,
                a2a_state=A2ATaskState.input_required,
                pending_confirmation=pending,
            )
        request = self._task_request(
            context=context,
            user_text=getattr(incoming, "text", context.current_goal),
            user_id=getattr(incoming, "user_id", context.user_id),
            channel_value=getattr(getattr(incoming, "channel", context.channel), "value", str(context.channel)),
            agent=agent,
            skill=skill,
            task_input=task_input,
            policy_decision=policy_decision,
            previous_outputs=previous_outputs,
            source_artifacts=source_artifacts,
            reference_task_ids=list(state.task_ids.values()),
            metadata=getattr(incoming, "metadata", {}),
        )
        try:
            result = await self.a2a_client.send_task(agent, request)
        except Exception as exc:
            logger.exception(
                "pi_tool_loop_a2a_failed context_id=%s skill=%s agent_id=%s endpoint=%s task_id=%s",
                context.context_id,
                skill,
                agent.agent_id,
                agent.endpoint,
                request.task_id,
            )
            result = _failed_agent_result(request=request, agent=agent, exc=exc)
        self._save_invocation(
            context=context,
            agent=agent,
            skill=skill,
            result=result,
            policy_decision=policy_decision,
            request_summary=getattr(incoming, "text", context.current_goal),
        )
        state.selected_agents.append({"skill": skill, "agent_id": agent.agent_id, "task_id": result.task_id})
        state.task_ids[skill] = result.task_id
        state.task_states[skill] = result.state.value
        state.artifacts.extend(result.artifacts)
        state.source_artifacts.extend(_artifact_references(result, skill))
        if result.structured_output:
            state.structured_outputs[skill] = result.structured_output
        return AgentObservation(
            tool_name="call_agent",
            status=result.state.value,
            content={
                "skill": skill,
                "agent_id": agent.agent_id,
                "task_id": result.task_id,
                "state": result.state.value,
                "text": _last_text(result),
                "structured_output": result.structured_output,
                "source_artifacts": _artifact_references(result, skill),
            },
        )

    def _request_confirmation(
        self,
        args: dict[str, Any],
        *,
        state: AgentTurnState,
        context: PIContext,
    ) -> AgentFinal:
        agent_id = str(args.get("agent_id") or "")
        skill = str(args.get("skill") or "")
        agent = state.discovered_agents.get(agent_id)
        if agent is None:
            return self._final(
                state,
                text=f"Cannot request confirmation because agent {agent_id!r} was not discovered.",
                a2a_state=A2ATaskState.input_required,
                errors=["undiscovered_agent_confirmation"],
            )
        pending = {
            "runtime": "tool_loop",
            "skill": skill,
            "agent_id": agent.agent_id,
            "policy_decision": PolicyDecisionStatus.requires_confirmation.value,
            "confirmation_status": ConfirmationStatus.pending.value,
            "reason": str(args.get("action_summary") or f"{skill} requires confirmation."),
            "selected_agent": agent.model_dump(mode="json"),
            "task_input": args.get("task_input") if isinstance(args.get("task_input"), dict) else {},
            "previous_outputs": {},
            "source_artifacts": [],
        }
        self.store.save_pending_confirmation(context.context_id, pending)
        state.selected_agents.append({"skill": skill, "agent_id": agent.agent_id, "task_id": None})
        return self._final(
            state,
            text=pending["reason"],
            a2a_state=A2ATaskState.input_required,
            pending_confirmation=pending,
        )

    def _task_request(
        self,
        *,
        context: PIContext,
        user_text: str,
        user_id: str,
        channel_value: str,
        agent: DiscoveredAgent,
        skill: str,
        task_input: dict[str, Any],
        policy_decision: PolicyDecision,
        previous_outputs: dict[str, object],
        source_artifacts: list[dict[str, str]],
        reference_task_ids: list[str],
        confirmed: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> A2ATaskRequest:
        safe_metadata = {
            key: value
            for key, value in (metadata or {}).items()
            if key not in {"user_id", "channel", "policy_decision", "confirmation_status", "confirmed", "skill"}
        }
        merged_metadata = {
            "user_id": user_id,
            "channel": channel_value,
            "policy_decision": policy_decision.status.value,
            "confirmation_status": policy_decision.confirmation_status.value,
            **safe_metadata,
            **task_input,
        }
        if confirmed:
            merged_metadata["confirmed"] = True
            merged_metadata["confirmation_status"] = ConfirmationStatus.confirmed.value
        if previous_outputs:
            merged_metadata["previous_outputs"] = previous_outputs
        if source_artifacts:
            merged_metadata["source_artifacts"] = source_artifacts
        if reference_task_ids:
            merged_metadata["reference_task_ids"] = reference_task_ids
        parts = [A2APart(kind="text", text=user_text)]
        task_data = {
            "skill": skill,
            "task_input": task_input,
            "previous_outputs": previous_outputs,
            "source_artifacts": source_artifacts,
        }
        parts.append(A2APart(kind="data", data=task_data))
        return A2ATaskRequest(
            task_id=str(uuid4()),
            context_id=context.context_id,
            target_agent_id=agent.agent_id,
            skill=skill,
            user_goal=user_text,
            messages=[A2AMessage(role="user", parts=parts)],
            metadata=merged_metadata,
        )

    def _save_invocation(
        self,
        *,
        context: PIContext,
        agent: DiscoveredAgent,
        skill: str,
        result: A2ATaskResult,
        policy_decision: PolicyDecision,
        request_summary: str,
    ) -> None:
        self.store.save_invocation(
            AgentInvocationRecord(
                context_id=context.context_id,
                task_id=result.task_id,
                agent_id=agent.agent_id,
                skill=skill,
                request_summary=request_summary,
                status=result.state,
                artifacts=result.artifacts,
                policy_decision=policy_decision,
                selected_agent=agent,
                completed_at=result.messages[-1].created_at if result.messages else None,
                error=result.error,
            )
        )
        context.active_tasks.append(result.task_id)
        self.store.save_context(context)

    def _final(
        self,
        state: AgentTurnState,
        *,
        text: str,
        a2a_state: A2ATaskState,
        pending_confirmation: dict[str, Any] | None = None,
        errors: list[str] | None = None,
    ) -> AgentFinal:
        return AgentFinal(
            context_id=state.context_id,
            state=a2a_state,
            text=text,
            llm_model=self.llm_config.model,
            selected_agents=state.selected_agents,
            workflow=[step["skill"] for step in state.selected_agents if step.get("skill")],
            task_ids=state.task_ids,
            task_states=state.task_states,
            policy_decisions=state.policy_decisions,
            confirmation_required=pending_confirmation is not None,
            pending_confirmation=pending_confirmation,
            artifacts=state.artifacts,
            source_artifacts=state.source_artifacts,
            structured_outputs=state.structured_outputs,
            structured_output=next(reversed(state.structured_outputs.values()), None),
            tool_trace=[observation.model_dump(mode="json") for observation in state.observations],
            errors=[*state.errors, *(errors or [])],
        )


def _tool_loop_system_prompt() -> str:
    return (
        "You are PI, a standard tool-loop agent. Work step by step and call exactly one "
        "tool at a time. ADS is the discovery service; A2A is the task transport. Use "
        "the ads_skill_catalog in the user payload to choose likely skill ids, then call "
        "discover_agents with that skill_hint. If the user intent is broad or the skill is "
        "unclear, call discover_agents with skill_hint=null to list routed ADS agents. "
        "After discovery, inspect_agent when you need capability details, examples, risk, "
        "or input shape. Call call_agent only with an agent_id returned by discovery and a "
        "skill advertised by that Agent Card. Never invent endpoint URLs, never call an "
        "undiscovered agent, and never treat ADS record storage as proof of live discovery. "
        "Policy is enforced by Python tools. For any send, create, append, replace, delete, "
        "repo mutation, command execution, or other side effect, call_agent may return "
        "confirmation_required; stop and wait for user confirmation. Do not claim writes "
        "or sends happened unless a tool observation proves it. Use read_context for follow-up "
        "questions, prior outputs, artifacts, task ids, and pending actions. Call final_answer "
        "only when the answer is supported by context or tool observations."
    )


def _initial_llm_payload(state: AgentTurnState) -> dict[str, Any]:
    return {
        "user_text": state.user_text,
        "requested_skill": state.requested_skill,
        "context_id": state.context_id,
        "ads_usage": {
            "directory": "Use discover_agents. It searches ADS through DirectoryClient.",
            "transport": "Use call_agent only after discovery. It invokes A2A through policy.",
            "skill_hint": "Prefer exact skill ids from ads_skill_catalog. Use null for broad discovery.",
            "safety": "Raw endpoint URLs are forbidden. Side effects require confirmation.",
        },
        "ads_skill_catalog": state.ads_skill_catalog,
        "ads_catalog_error": state.ads_catalog_error,
        "instructions": "Call exactly one tool next.",
    }


def _tool_schemas() -> list[dict[str, Any]]:
    return [
        _function_tool(
            "read_context",
            "Read PI conversation memory, prior tool observations, artifacts, and pending actions.",
            {},
        ),
        _function_tool(
            "discover_agents",
            "Search ADS through DirectoryClient for capable A2A agents.",
            {
                "query": {"type": "string"},
                "skill_hint": {"type": ["string", "null"]},
            },
        ),
        _function_tool(
            "inspect_agent",
            "Inspect a previously discovered agent's safe skill metadata.",
            {"agent_id": {"type": "string"}},
        ),
        _function_tool(
            "call_agent",
            "Call a discovered A2A agent skill. Python policy may block or require confirmation.",
            {
                "agent_id": {"type": "string"},
                "skill": {"type": "string"},
                "task_input": {"type": "object", "additionalProperties": True},
            },
            strict=False,
        ),
        _function_tool(
            "request_confirmation",
            "Ask for user confirmation before a side-effecting action.",
            {
                "action_summary": {"type": "string"},
                "agent_id": {"type": "string"},
                "skill": {"type": "string"},
                "task_input": {"type": "object", "additionalProperties": True},
            },
            strict=False,
        ),
        _function_tool(
            "ask_user",
            "Ask the user a clarifying question and stop this turn.",
            {"text": {"type": "string"}},
        ),
        _function_tool(
            "final_answer",
            "Return the final answer to the user and stop this turn.",
            {"text": {"type": "string"}},
        ),
    ]


def _function_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties.keys()),
        },
        "strict": strict,
    }


def _tool_call_from_response(response: Any) -> AgentToolCall:
    output = response.get("output") if isinstance(response, dict) else getattr(response, "output", None)
    for item in output or []:
        item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        if item_type != "function_call":
            continue
        name = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
        raw_args = item.get("arguments") if isinstance(item, dict) else getattr(item, "arguments", "{}")
        call_id = item.get("call_id") if isinstance(item, dict) else getattr(item, "call_id", None)
        try:
            args = json.loads(raw_args or "{}") if isinstance(raw_args, str) else dict(raw_args or {})
        except json.JSONDecodeError:
            args = {"_invalid_arguments": str(raw_args)}
        return AgentToolCall(tool_name=str(name), arguments=args, call_id=call_id)
    text = _response_text(response)
    return AgentToolCall(tool_name="final_answer", arguments={"text": text})


def _default_openai_client(config: PILLMConfig) -> Any:
    try:
        from openai import AsyncOpenAI
    except Exception as exc:
        raise PILLMPlannerError(f"OpenAI SDK import failed: {type(exc).__name__}: {exc}") from exc
    return AsyncOpenAI(timeout=config.timeout_seconds)


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
    chunks: list[str] = []
    for item in output or []:
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        for part in content or []:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if text:
                chunks.append(str(text))
    return "".join(chunks)


def _context_snapshot(context: PIContext, store: InMemoryStateStore) -> dict[str, Any]:
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
            }
            for invocation in invocations[-12:]
        ],
        "previous_outputs": previous_outputs,
        "source_artifacts": source_artifacts,
    }


def _ads_skill_catalog(agents: list[DiscoveredAgent]) -> list[dict[str, Any]]:
    catalog: dict[tuple[str, str], dict[str, Any]] = {}
    for agent in agents:
        for capability in agent.skills:
            for skill in _skill_ids_for_capability(capability):
                catalog[(skill, agent.agent_id)] = {
                    "skill": skill,
                    "agent_id": agent.agent_id,
                    "agent_name": agent.name,
                    "organization": agent.organization,
                    "description": capability.description or agent.description,
                    "risk_level": capability.risk_level.value,
                    "action_type": capability.action_type,
                    "input_modes": capability.input_modes or agent.input_modes,
                    "output_modes": capability.output_modes or agent.output_modes,
                    "examples": capability.examples,
                    "source_record_id": agent.source_record_id,
                }
    return sorted(catalog.values(), key=lambda item: (item["agent_id"], item["skill"]))


def _skill_ids_for_capability(capability: AgentCapability) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *capability.skill_tags,
                *capability.custom_skill_tags,
                *(item.name for item in capability.official_oasf_skills),
            ]
        )
    )


def _safe_agent(agent: DiscoveredAgent, *, include_details: bool = False) -> dict[str, Any]:
    payload = {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "organization": agent.organization,
        "description": agent.description,
        "trust_level": agent.trust_level,
        "source_record_id": agent.source_record_id,
        "skills": [
            {
                "name": capability.name,
                "description": capability.description,
                "skill_tags": capability.skill_tags,
                "custom_skill_tags": capability.custom_skill_tags,
                "risk_level": capability.risk_level.value,
                "action_type": capability.action_type,
                "input_modes": capability.input_modes,
                "output_modes": capability.output_modes,
                "examples": capability.examples,
            }
            for capability in agent.skills
        ],
    }
    if include_details:
        payload["auth"] = agent.auth
        payload["required_scopes"] = agent.required_scopes
    return payload


def _capability_for_skill(agent: DiscoveredAgent, skill: str) -> AgentCapability:
    for capability in agent.skills:
        official_names = {item.name for item in capability.official_oasf_skills}
        if skill in set(capability.skill_tags) | set(capability.custom_skill_tags) | official_names:
            return capability
    return agent.skills[0]


def _policy_entry(skill: str, agent: DiscoveredAgent, policy_decision: PolicyDecision) -> dict[str, str]:
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
) -> dict[str, Any]:
    return {
        "skill": skill,
        "agent_id": agent.agent_id,
        "policy_decision": policy_decision.status.value,
        "confirmation_status": policy_decision.confirmation_status.value,
        "reason": policy_decision.reason,
    }


def _outputs_from_invocations(invocations: list[AgentInvocationRecord]) -> tuple[dict[str, object], list[dict[str, str]]]:
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


def _failed_agent_result(
    *,
    request: A2ATaskRequest,
    agent: DiscoveredAgent,
    exc: BaseException,
) -> A2ATaskResult:
    message = (
        f"A2A call to {agent.name or agent.agent_id} for {request.skill} failed: "
        f"{type(exc).__name__}: {exc}"
    )
    return A2ATaskResult(
        task_id=request.task_id or str(uuid4()),
        context_id=request.context_id,
        state=A2ATaskState.failed,
        messages=[A2AMessage(role="agent", parts=[A2APart(kind="text", text=message)])],
        structured_output={
            "skill": request.skill,
            "agent_id": agent.agent_id,
            "endpoint": agent.endpoint,
            "status": "a2a_call_failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        },
        error=message,
    )


def _draft_email_from_outputs(outputs: dict[str, object]) -> dict[str, object] | None:
    for output in outputs.values():
        if isinstance(output, dict) and isinstance(output.get("draft_email"), dict):
            return output["draft_email"]
    return None


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


def _looks_like_endpoint_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
