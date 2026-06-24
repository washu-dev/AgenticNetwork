from __future__ import annotations

import base64
import inspect
import logging
import json
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pydantic import BaseModel, Field

from planner_agent.main_agent.a2a.client import A2AClient
from planner_agent.main_agent.models import (
    A2AMessage,
    A2APart,
    A2ATaskRequest,
    A2ATaskResult,
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
from planner_agent.main_agent.directory.client import DirectoryClient
from planner_agent.main_agent.directory.backends import DirectoryUnavailable
from planner_agent.main_agent.state.store import InMemoryStateStore

from planner_agent.main_agent.config import MainAgentConfig
from planner_agent.main_agent.llm_client import LLMClient, ToolCall, TurnState

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


# Skills we treat as side-effecting in strict policy mode.
WRITE_SKILL_TOKENS = (
    "send",
    "create",
    "update",
    "delete",
    "write",
    "append",
    "replace",
    "publish",
    "execute",
    "fix",
    "modify",
)


class TurnResult(BaseModel):
    context_id: str
    state: A2ATaskState
    text: str
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    selected_agents: list[dict[str, Any]] = Field(default_factory=list)
    confirmation_required: bool = False
    pending_confirmation: dict[str, Any] | None = None
    structured_outputs: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    llm_model: str | None = None

    def to_response(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "state": self.state.value,
            "text": self.text,
            "final_response": self.text,
            "tool_trace": self.tool_trace,
            "selected_agents": self.selected_agents,
            "selected_agent": self.selected_agents[-1]["agent_id"] if self.selected_agents else None,
            "confirmation_required": self.confirmation_required,
            "pending_confirmation": self.pending_confirmation,
            "structured_outputs": self.structured_outputs,
            "structured_output": next(reversed(self.structured_outputs.values()), None),
            "errors": self.errors,
            "llm_model": self.llm_model,
        }


class MainAgentRuntime:
    """Tool-loop main agent.

    Owns ADS discovery, A2A transport, conversation memory, and policy gating.
    The LLM only sees TurnState (safe view) and chooses one tool per step.
    """

    def __init__(
        self,
        *,
        config: MainAgentConfig,
        directory_client: DirectoryClient,
        a2a_client: A2AClient,
        state_store: InMemoryStateStore,
        llm_client: LLMClient,
    ):
        self.config = config
        self.directory = directory_client
        self.a2a_client = a2a_client
        self.store = state_store
        self.llm_client = llm_client

    # -------- public API --------

    async def handle_message(
        self,
        *,
        user_id: str,
        channel: Channel,
        text: str,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> TurnResult:
        if context_id and self._is_affirmative(text):
            pending = self.store.get_pending_confirmation(context_id)
            if pending:
                return await self._resume_confirmation(
                    context_id, user_id, channel, metadata or {}, user_text=text,
                )
        if context_id and self._is_negative(text):
            pending = self.store.get_pending_confirmation(context_id)
            if pending:
                return self._cancel_confirmation(context_id, pending, user_text=text)

        context = self._open_context(user_id, channel, text, context_id)
        uploaded_files = _uploaded_files_from_metadata(metadata or {})
        credentials = (metadata or {}).get("credentials") or {}
        state = TurnState(
            context_id=context.context_id,
            user_text=text,
            conversation_history=list(context.conversation_history)[-self.config.history_limit :],
            pending_confirmation=self.store.get_pending_confirmation(context.context_id),
            uploaded_files=uploaded_files,
        )

        result = await self._run_loop(
            state=state,
            context=context,
            credentials=credentials,
            progress_callback=progress_callback,
        )
        # Append assistant final text to conversation history.
        if result.text and result.state in {A2ATaskState.completed, A2ATaskState.input_required}:
            entry = f"assistant: {result.text}"
            if not context.conversation_history or context.conversation_history[-1] != entry:
                context.conversation_history.append(entry)
            context.updated_at = utc_now()
            self.store.save_context(context)
        return result

    # -------- core loop --------

    async def _run_loop(
        self,
        *,
        state: TurnState,
        context: PIContext,
        credentials: dict[str, Any] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> TurnResult:
        tool_trace: list[dict[str, Any]] = []
        selected_agents: list[dict[str, Any]] = []
        structured_outputs: dict[str, Any] = {}
        # Per-turn request fingerprints for calls that completed successfully.
        # Skill is now advisory, so dedup keys on the actual request shape
        # rather than just (agent_id, skill). Failed calls intentionally skip
        # this gate so the LLM can retry transient A2A failures.
        successful_calls: set[str] = set()
        errors: list[str] = []
        # Cache the most recent list_agents result so describe_agent / call_agent
        # can resolve agent_id without re-querying ADS.
        discovered: dict[str, DiscoveredAgent] = {}

        for step in range(self.config.max_steps):
            state.step = step
            try:
                tool_call = await self.llm_client.next_tool_call(state)
            except Exception as exc:
                logger.exception("main_agent_llm_failed context_id=%s", context.context_id)
                errors.append(f"llm_error: {type(exc).__name__}: {exc}")
                return TurnResult(
                    context_id=context.context_id,
                    state=A2ATaskState.failed,
                    text=f"LLM call failed: {exc}",
                    tool_trace=tool_trace,
                    selected_agents=selected_agents,
                    structured_outputs=structured_outputs,
                    errors=errors,
                    llm_model=self.config.llm_model,
                )

            logger.info(
                "main_agent_step context_id=%s step=%d tool=%s reasoning_chars=%d",
                context.context_id,
                step,
                tool_call.tool_name,
                len(tool_call.reasoning or ""),
            )

            if tool_call.reasoning:
                await _record_trace(
                    tool_trace,
                    {
                        "tool": "reasoning",
                        "status": "ok",
                        "content": {"text": tool_call.reasoning, "next_tool": tool_call.tool_name},
                    },
                    progress_callback,
                )

            terminal = await self._dispatch_tool(
                tool_call=tool_call,
                state=state,
                context=context,
                tool_trace=tool_trace,
                selected_agents=selected_agents,
                structured_outputs=structured_outputs,
                successful_calls=successful_calls,
                discovered=discovered,
                credentials=credentials or {},
                progress_callback=progress_callback,
            )
            if terminal is not None:
                return terminal

        # Loop exhausted without final_answer.
        text = "I reached the maximum number of tool steps before producing a final answer."
        return TurnResult(
            context_id=context.context_id,
            state=A2ATaskState.input_required,
            text=text,
            tool_trace=tool_trace,
            selected_agents=selected_agents,
            structured_outputs=structured_outputs,
            errors=[*errors, "max_steps_exceeded"],
            llm_model=self.config.llm_model,
        )

    async def _dispatch_tool(
        self,
        *,
        tool_call: ToolCall,
        state: TurnState,
        context: PIContext,
        tool_trace: list[dict[str, Any]],
        selected_agents: list[dict[str, Any]],
        structured_outputs: dict[str, Any],
        successful_calls: set[str],
        discovered: dict[str, DiscoveredAgent],
        credentials: dict[str, Any] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> TurnResult | None:
        name = tool_call.tool_name
        args = dict(tool_call.arguments or {})

        if name == "final_answer":
            text = str(args.get("text") or "").strip()
            if not text:
                text = _salvage_final_text(state, structured_outputs)
            if _is_confused_response(text) and state.step < self.config.max_steps - 1:
                # Model said "I'm not sure" or similar — inject as an observation
                # and loop back so it tries again with an explicit nudge.
                nudge_obs = {
                    "tool": "final_answer",
                    "status": "rejected",
                    "content": {
                        "reason": (
                            "Your final_answer text was not helpful. "
                            "Look at the tool observations already in your context and write "
                            "a complete, informative answer based on what you found."
                        )
                    },
                }
                await _record_trace(tool_trace, nudge_obs, progress_callback)
                state.observations.append(nudge_obs)
                state.messages.append({"role": "tool", "tool_call_id": tool_call.call_id or "nudge", "content": nudge_obs["content"]["reason"]})
                return None
            await _record_trace(
                tool_trace,
                _with_token_usage({"tool": name, "status": "ok", "args": args}, tool_call),
                progress_callback,
            )
            return TurnResult(
                context_id=context.context_id,
                state=A2ATaskState.completed,
                text=text,
                tool_trace=tool_trace,
                selected_agents=selected_agents,
                structured_outputs=structured_outputs,
                llm_model=self.config.llm_model,
            )

        if name == "ask_user":
            text = str(args.get("text") or "Could you clarify?")
            await _record_trace(
                tool_trace,
                _with_token_usage({"tool": name, "status": "ok", "args": args}, tool_call),
                progress_callback,
            )
            return TurnResult(
                context_id=context.context_id,
                state=A2ATaskState.input_required,
                text=text,
                tool_trace=tool_trace,
                selected_agents=selected_agents,
                structured_outputs=structured_outputs,
                llm_model=self.config.llm_model,
            )

        if name == "list_agents":
            obs = self._tool_list_agents(discovered)
        elif name == "describe_agent":
            obs = self._tool_describe_agent(args, discovered)
        elif name == "call_agent":
            obs, pending = await self._tool_call_agent(
                args=args,
                state=state,
                context=context,
                tool_trace=tool_trace,
                discovered=discovered,
                selected_agents=selected_agents,
                structured_outputs=structured_outputs,
                successful_calls=successful_calls,
                credentials=credentials or {},
                progress_callback=progress_callback,
            )
            await _record_trace(
                tool_trace,
                _with_token_usage(
                    {"tool": name, "status": obs["status"], "content": obs["content"]},
                    tool_call,
                ),
                progress_callback,
            )
            state.observations.append(obs)
            if pending is not None:
                # confirmation_required: build the terminal TurnResult HERE
                # so it carries the full accumulated tool_trace, not just [].
                return TurnResult(
                    context_id=context.context_id,
                    state=A2ATaskState.input_required,
                    text=pending["text"],
                    tool_trace=list(tool_trace),
                    selected_agents=list(selected_agents),
                    confirmation_required=True,
                    pending_confirmation=pending["pending_confirmation"],
                    structured_outputs=dict(structured_outputs),
                    llm_model=self.config.llm_model,
                )
            return None
        else:
            obs = {"tool": name, "status": "error", "content": {"error": f"Unknown tool {name!r}."}}

        await _record_trace(tool_trace, _with_token_usage(obs, tool_call), progress_callback)
        # Trim observation content for state — LLM sees the same info, no transformations.
        state.observations.append(obs)
        return None

    # -------- tool implementations --------

    def _tool_list_agents(self, discovered: dict[str, DiscoveredAgent]) -> dict[str, Any]:
        try:
            agents = self.directory.search_agents()
        except DirectoryUnavailable as exc:
            return {
                "tool": "list_agents",
                "status": "error",
                "content": {"error": str(exc)},
            }
        if not agents:
            return {
                "tool": "list_agents",
                "status": "ok",
                "content": {"agents": [], "note": "ADS returned zero agents."},
            }
        for agent in agents:
            discovered[agent.agent_id] = agent
        return {
            "tool": "list_agents",
            "status": "ok",
            "content": {
                "agents": [_agent_summary(agent) for agent in agents],
                "count": len(agents),
            },
        }

    def _tool_describe_agent(
        self,
        args: dict[str, Any],
        discovered: dict[str, DiscoveredAgent],
    ) -> dict[str, Any]:
        agent_id = str(args.get("agent_id") or "").strip()
        if not agent_id:
            return _err("describe_agent", "agent_id is required.")
        agent = self._resolve_agent_lenient(agent_id, discovered)
        if agent is None:
            return _err(
                "describe_agent",
                f"Agent {agent_id!r} not found in ADS. "
                f"Call list_agents first and reuse the returned agent_id values.",
            )
        return {
            "tool": "describe_agent",
            "status": "ok",
            "content": _agent_full_card(agent),
        }

    async def _tool_call_agent(
        self,
        *,
        args: dict[str, Any],
        state: TurnState,
        context: PIContext,
        tool_trace: list[dict[str, Any]],
        discovered: dict[str, DiscoveredAgent],
        selected_agents: list[dict[str, Any]],
        structured_outputs: dict[str, Any],
        successful_calls: set[str],
        credentials: dict[str, Any] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        agent_id = str(args.get("agent_id") or "").strip()
        requested_skill = str(args.get("skill") or "").strip() or None
        task_input = args.get("task_input") if isinstance(args.get("task_input"), dict) else {}

        if not agent_id:
            return _err("call_agent", "agent_id is required."), None
        if "://" in agent_id or (requested_skill and "://" in requested_skill):
            return _err("call_agent", "URLs are not accepted; use discovered agent_id and optional skill hint."), None

        logger.info(
            "main_agent_call_agent_args context_id=%s agent_id=%s requested_skill=%s task_input_keys=%s",
            context.context_id,
            agent_id,
            requested_skill,
            sorted(task_input.keys()) if task_input else [],
        )

        # Belt-and-braces: if the LLM forgot to fill task_input for an email-like skill,
        # extract recipient/subject/body from the user's text and conversation history
        # so the email agent doesn't bounce on "missing recipient address".
        agent = self._resolve_agent_lenient(agent_id, discovered)
        if agent is None:
            return (
                _err(
                    "call_agent",
                    f"Agent {agent_id!r} is not in ADS. "
                    f"Use list_agents first; pass the agent_id from the result, not a display name.",
                ),
                None,
            )

        if requested_skill and _is_email_skill(requested_skill):
            task_input = _augment_email_task_input(
                task_input=task_input,
                user_text=state.user_text,
                conversation_history=context.conversation_history,
            )
        elif not requested_skill and _agent_has_email_skills(agent):
            task_input = _augment_email_task_input(
                task_input=task_input,
                user_text=state.user_text,
                conversation_history=context.conversation_history,
            )

        canonical_hint = (
            _canonical_skill(agent, requested_skill)
            if requested_skill
            else None
        )
        capability = _capability_for_skill(agent, canonical_hint) if canonical_hint else None
        if requested_skill and canonical_hint is None:
            logger.info(
                "main_agent_skill_hint_not_advertised context_id=%s agent_id=%s requested_skill=%s advertised=%s",
                context.context_id,
                agent.agent_id,
                requested_skill,
                sorted(_advertised_skills(agent)),
            )

        fingerprint = _request_fingerprint(
            agent_id=agent.agent_id,
            user_text=state.user_text,
            task_input=task_input,
            requested_skill=requested_skill,
        )
        if fingerprint in successful_calls:
            prior_task = next(
                (
                    step["task_id"]
                    for step in selected_agents
                    if step.get("agent_id") == agent.agent_id
                    and step.get("request_fingerprint") == fingerprint
                    and step.get("task_id")
                ),
                None,
            )
            return (
                {
                    "tool": "call_agent",
                    "status": "skipped",
                    "content": {
                        "requested_skill": requested_skill,
                        "skill_hint": requested_skill,
                        "chosen_skill": None,
                        "agent_id": agent.agent_id,
                        "reason": (
                            f"This request already ran successfully on {agent.agent_id} "
                            "in this turn. The previous structured_output is in your context — "
                            "do NOT call it again; call final_answer with the result."
                        ),
                        "previous_task_id": prior_task,
                    },
                },
                None,
            )

        prior_dispatch = _first_completed_dispatch(selected_agents)
        if prior_dispatch is not None and prior_dispatch.get("agent_id") == agent.agent_id:
            if not _prior_dispatch_reports_failed_handoff(structured_outputs):
                return (
                    {
                        "tool": "call_agent",
                        "status": "skipped",
                        "content": {
                            "requested_skill": requested_skill,
                            "skill_hint": requested_skill,
                            "chosen_skill": None,
                            "agent_id": agent.agent_id,
                            "reason": (
                                "The main agent already dispatched this user task to "
                                f"{prior_dispatch.get('agent_id')}. External agents are responsible "
                                "for any peer A2A coordination; call final_answer using the existing "
                                "structured_output instead of dispatching another agent."
                            ),
                            "previous_agent_id": prior_dispatch.get("agent_id"),
                            "previous_task_id": prior_dispatch.get("task_id"),
                        },
                    },
                    None,
                )
            logger.info(
                "main_agent_followup_dispatch_after_failed_peer_handoff context_id=%s prior_agent_id=%s next_agent_id=%s",
                context.context_id,
                prior_dispatch.get("agent_id"),
                agent.agent_id,
            )

        decision = self._evaluate_policy(canonical_hint, capability)
        if decision.status == PolicyDecisionStatus.requires_confirmation:
            pending = {
                "skill": canonical_hint,
                "requested_skill": requested_skill,
                "skill_hint": requested_skill,
                "agent_id": agent.agent_id,
                "task_input": task_input,
                "reason": decision.reason,
                "policy_decision": decision.status.value,
                "confirmation_status": ConfirmationStatus.pending.value,
                "selected_agent": agent.model_dump(mode="json"),
            }
            self.store.save_pending_confirmation(context.context_id, pending)
            selected_agents.append(
                {
                    "skill": canonical_hint,
                    "requested_skill": requested_skill,
                    "chosen_skill": None,
                    "agent_id": agent.agent_id,
                    "task_id": None,
                    "request_fingerprint": fingerprint,
                    "state": "confirmation_required",
                }
            )
            obs = {
                "tool": "call_agent",
                "status": "confirmation_required",
                "content": {
                    "requested_skill": requested_skill,
                    "skill_hint": requested_skill,
                    "chosen_skill": None,
                    "agent_id": agent.agent_id,
                    "reason": decision.reason,
                    "task_input": task_input,
                },
            }
            terminal_payload = {
                "text": (
                    f"This action ({canonical_hint}) needs your confirmation. "
                    f"Reply 'yes' to proceed, 'no' to cancel."
                ),
                "pending_confirmation": pending,
            }
            return obs, terminal_payload

        # Allowed → invoke A2A.
        request = self._build_a2a_request(
            context=context,
            agent=agent,
            skill=requested_skill,
            task_input=task_input,
            user_text=state.user_text,
            confirmed=False,
            uploaded_files=state.uploaded_files,
            credentials=credentials or {},
        )
        remote_progress_callback = None
        if progress_callback is not None:
            async def remote_progress_callback(entry: dict[str, Any]) -> None:
                await _record_trace(tool_trace, entry, progress_callback)

        result = await self._send_task(agent, request, progress_callback=remote_progress_callback)
        chosen_skill = _chosen_skill_from_result(result, requested_skill=canonical_hint or requested_skill)
        record_skill = chosen_skill or canonical_hint or requested_skill or "agent/autonomous"
        self._save_invocation(context, agent, record_skill, result, decision)
        selected_agents.append(
            {
                "skill": record_skill,
                "requested_skill": requested_skill,
                "chosen_skill": chosen_skill,
                "agent_id": agent.agent_id,
                "task_id": result.task_id,
                "request_fingerprint": fingerprint,
                "state": result.state.value,
            }
        )
        if result.structured_output:
            structured_outputs[record_skill] = result.structured_output
        if result.state == A2ATaskState.completed:
            successful_calls.add(fingerprint)

        obs = {
            "tool": "call_agent",
            "status": result.state.value,
            "content": {
                "skill": record_skill,
                "requested_skill": requested_skill,
                "skill_hint": requested_skill,
                "chosen_skill": chosen_skill,
                "agent_id": agent.agent_id,
                "task_id": result.task_id,
                "state": result.state.value,
                "text": _last_text(result),
                "structured_output": result.structured_output,
            },
        }
        return obs, None

    def _resolve_agent_lenient(
        self,
        identifier: str,
        discovered: dict[str, DiscoveredAgent],
    ) -> DiscoveredAgent | None:
        """Resolve an agent by id, source_record_id, or display name.

        LLMs (gpt-4o-mini in particular) sometimes pass the human-readable
        display name like "Disaster Response Agent" instead of the slug
        "disaster-response-agent". To save a tool round-trip, we accept any
        of: agent_id, record id, exact name, normalized name (case- and
        punctuation-insensitive), or substring of name.
        """
        if not identifier:
            return None
        if identifier in discovered:
            return discovered[identifier]
        try:
            agent = self.directory.resolve_agent(identifier)
            discovered[agent.agent_id] = agent
            return agent
        except (KeyError, DirectoryUnavailable):
            pass
        try:
            agents = self.directory.search_agents()
        except DirectoryUnavailable:
            return None
        for agent in agents:
            discovered.setdefault(agent.agent_id, agent)
        wanted = _normalize_agent_key(identifier)
        for agent in agents:
            if _normalize_agent_key(agent.name) == wanted:
                return agent
            if _normalize_agent_key(agent.agent_id) == wanted:
                return agent
        for agent in agents:
            if wanted and wanted in _normalize_agent_key(agent.name):
                return agent
        return None

    # -------- confirmation handling --------

    async def _resume_confirmation(
        self,
        context_id: str,
        user_id: str,
        channel: Channel,
        metadata: dict[str, Any],
        user_text: str = "yes",
    ) -> TurnResult:
        try:
            context = self.store.get_context(context_id)
        except KeyError:
            return TurnResult(
                context_id=context_id,
                state=A2ATaskState.failed,
                text=f"Unknown context_id {context_id}.",
                errors=["unknown_context"],
                llm_model=self.config.llm_model,
            )
        # Record the user's confirmation turn so future LLM turns can see it.
        context.conversation_history.append(f"user: {user_text}")
        context.updated_at = utc_now()
        pending = self.store.get_pending_confirmation(context_id) or {}
        agent_data = pending.get("selected_agent")
        if not agent_data:
            return TurnResult(
                context_id=context_id,
                state=A2ATaskState.failed,
                text="No pending confirmation found.",
                errors=["no_pending"],
                llm_model=self.config.llm_model,
            )
        agent = DiscoveredAgent.model_validate(agent_data)
        skill = str(pending.get("skill") or "")
        task_input = dict(pending.get("task_input") or {})

        request = self._build_a2a_request(
            context=context,
            agent=agent,
            skill=skill,
            task_input=task_input,
            user_text=context.current_goal,
            confirmed=True,
            uploaded_files=[],
            credentials=metadata.get("credentials") or {},
        )
        result = await self._send_task(agent, request)
        confirmed_decision = PolicyDecision(
            status=PolicyDecisionStatus.allowed,
            reason=f"User confirmed {skill}.",
            confirmation_status=ConfirmationStatus.confirmed,
        )
        self._save_invocation(context, agent, skill, result, confirmed_decision)
        self.store.clear_pending_confirmation(context_id)

        text = _last_text(result) or f"{skill} completed."
        if context.conversation_history is not None:
            context.conversation_history.append(f"assistant: {text}")
            context.updated_at = utc_now()
            self.store.save_context(context)

        return TurnResult(
            context_id=context_id,
            state=result.state,
            text=text,
            tool_trace=[
                {
                    "tool": "call_agent",
                    "status": result.state.value,
                    "content": {
                        "skill": skill,
                        "agent_id": agent.agent_id,
                        "task_id": result.task_id,
                        "confirmed": True,
                        "structured_output": result.structured_output,
                    },
                }
            ],
            selected_agents=[
                {"skill": skill, "agent_id": agent.agent_id, "task_id": result.task_id}
            ],
            structured_outputs=({skill: result.structured_output} if result.structured_output else {}),
            llm_model=self.config.llm_model,
        )

    def _cancel_confirmation(
        self,
        context_id: str,
        pending: dict[str, Any],
        user_text: str = "no",
    ) -> TurnResult:
        skill = str(pending.get("skill") or "pending_action")
        self.store.clear_pending_confirmation(context_id)
        text = f"Cancelled. {skill} was not executed."
        try:
            context = self.store.get_context(context_id)
            context.conversation_history.append(f"user: {user_text}")
            context.conversation_history.append(f"assistant: {text}")
            context.updated_at = utc_now()
            self.store.save_context(context)
        except KeyError:
            pass
        return TurnResult(
            context_id=context_id,
            state=A2ATaskState.canceled,
            text=text,
            llm_model=self.config.llm_model,
        )

    # -------- helpers --------

    def _open_context(
        self,
        user_id: str,
        channel: Channel,
        text: str,
        context_id: str | None,
    ) -> PIContext:
        context = self.store.get_server_context()
        if context is None:
            context = self._new_context(user_id, channel, text)
            return self.store.save_server_context(context)

        context.user_id = user_id
        context.channel = channel
        context.current_goal = text
        context.updated_at = utc_now()
        context.conversation_history.append(f"user: {text}")
        return self.store.save_server_context(context)

    def _new_context(self, user_id: str, channel: Channel, text: str) -> PIContext:
        msg = UserMessage(channel=channel, sender_id=user_id, text=text)
        return PIContext(
            context_id=str(uuid4()),
            user_id=user_id,
            channel=channel,
            original_message_id=msg.id,
            current_goal=text,
            conversation_history=[f"user: {text}"],
        )

    def _evaluate_policy(self, skill: str | None, capability: AgentCapability | None) -> PolicyDecision:
        if self.config.policy_mode in {"permissive", "off", "disabled"}:
            return PolicyDecision(
                status=PolicyDecisionStatus.allowed,
                reason=f"{skill or 'autonomous agent request'} allowed in permissive mode.",
                confirmation_status=ConfirmationStatus.not_required,
            )
        if not skill or capability is None:
            return PolicyDecision(
                status=PolicyDecisionStatus.allowed,
                reason="No definite write skill was requested; external agent may choose the operation.",
                confirmation_status=ConfirmationStatus.not_required,
            )
        if capability.action_type == "write" or _is_write_skill(skill):
            return PolicyDecision(
                status=PolicyDecisionStatus.requires_confirmation,
                reason=f"{skill} may produce side effects and requires confirmation.",
                confirmation_status=ConfirmationStatus.pending,
            )
        return PolicyDecision(
            status=PolicyDecisionStatus.allowed,
            reason=f"{skill} is read-only.",
            confirmation_status=ConfirmationStatus.not_required,
        )

    def _build_a2a_request(
        self,
        *,
        context: PIContext,
        agent: DiscoveredAgent,
        skill: str | None,
        task_input: dict[str, Any],
        user_text: str,
        confirmed: bool,
        uploaded_files: list[dict[str, Any]] | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> A2ATaskRequest:
        should_attach_files = bool(uploaded_files)
        outbound_task_input = dict(task_input)
        file_parts: list[A2APart] = []
        attached_files: list[dict[str, Any]] = []
        if should_attach_files:
            for file in (uploaded_files or [])[:1]:
                file_part = _uploaded_file_to_a2a_part(file)
                if file_part is None:
                    continue
                file_parts.append(file_part)
                attached_files.append(_uploaded_file_summary(file))
            if attached_files:
                first = attached_files[0]
                outbound_task_input.setdefault("file_filename", first.get("filename"))
                outbound_task_input.setdefault("file_content_type", first.get("content_type"))

        metadata: dict[str, Any] = {
            "user_id": context.user_id,
            "channel": context.channel.value,
            **{k: v for k, v in outbound_task_input.items() if k not in {"user_id", "channel", "skill"}},
        }
        if credentials:
            metadata["credentials"] = credentials
        if attached_files:
            metadata["uploaded_files"] = attached_files
            metadata["file_transfer"] = "a2a_file_bytes"
        if skill:
            metadata["skill"] = skill
            metadata["requested_skill"] = skill
            metadata["skill_hint"] = skill
        if confirmed:
            metadata["confirmed"] = True
            metadata["confirmation_status"] = ConfirmationStatus.confirmed.value
        parts = [A2APart(kind="text", text=user_text)]
        if outbound_task_input:
            parts.append(A2APart(kind="data", data=outbound_task_input))
        parts.extend(file_parts)
        return A2ATaskRequest(
            task_id=str(uuid4()),
            context_id=context.context_id,
            target_agent_id=agent.agent_id,
            skill=skill,
            user_goal=user_text,
            messages=[A2AMessage(role="user", parts=parts)],
            metadata=metadata,
        )

    async def _send_task(
        self,
        agent: DiscoveredAgent,
        request: A2ATaskRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> A2ATaskResult:
        try:
            send_task = self.a2a_client.send_task
            if progress_callback is not None and "progress_callback" in inspect.signature(send_task).parameters:
                return await send_task(agent, request, progress_callback=progress_callback)
            return await send_task(agent, request)
        except Exception as exc:
            logger.exception(
                "main_agent_a2a_failed agent_id=%s skill=%s endpoint=%s",
                agent.agent_id,
                request.skill,
                agent.endpoint,
            )
            text = (
                f"A2A call to {agent.name} for {request.skill} failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return A2ATaskResult(
                task_id=request.task_id or str(uuid4()),
                context_id=request.context_id,
                state=A2ATaskState.failed,
                messages=[A2AMessage(role="agent", parts=[A2APart(kind="text", text=text)])],
                structured_output={
                    "skill": request.skill,
                    "requested_skill": request.skill,
                    "skill_hint": request.skill,
                    "chosen_skill": None,
                    "agent_id": agent.agent_id,
                    "status": "a2a_call_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                error=text,
            )

    def _save_invocation(
        self,
        context: PIContext,
        agent: DiscoveredAgent,
        skill: str,
        result: A2ATaskResult,
        decision: PolicyDecision,
    ) -> None:
        self.store.save_invocation(
            AgentInvocationRecord(
                context_id=context.context_id,
                task_id=result.task_id,
                agent_id=agent.agent_id,
                skill=skill,
                request_summary=context.current_goal,
                status=result.state,
                artifacts=result.artifacts,
                policy_decision=decision,
                selected_agent=agent,
                completed_at=result.messages[-1].created_at if result.messages else None,
                error=result.error,
            )
        )
        context.active_tasks.append(result.task_id)
        self.store.save_context(context)

    @staticmethod
    def _is_affirmative(text: str) -> bool:
        return _normalize(text) in {
            "yes", "y", "confirm", "confirmed", "go ahead", "send", "send it", "ok", "okay",
        }

    @staticmethod
    def _is_negative(text: str) -> bool:
        return _normalize(text) in {
            "no", "n", "cancel", "stop", "abort", "do not send", "don't send",
        }


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().replace(".", "").replace("!", "").split())


async def _record_trace(
    tool_trace: list[dict[str, Any]],
    entry: dict[str, Any],
    progress_callback: ProgressCallback | None,
) -> None:
    tool_trace.append(entry)
    if progress_callback is None:
        return
    try:
        await progress_callback(entry)
    except Exception:
        logger.exception("main_agent_progress_callback_failed")


def _with_token_usage(entry: dict[str, Any], tool_call: ToolCall) -> dict[str, Any]:
    if not tool_call.token_usage:
        return entry
    return {**entry, "token_usage": tool_call.token_usage}


def _normalize_agent_key(value: str) -> str:
    """Lowercase + strip non-alphanumerics so that 'Disaster Response Agent',
    'disaster-response-agent', 'disaster_response_agent', and 'DisasterResponseAgent'
    all collapse to the same key."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _first_completed_dispatch(selected_agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    for step in selected_agents:
        if step.get("state") == A2ATaskState.completed.value and step.get("task_id"):
            return step
    return None


def _prior_dispatch_reports_failed_handoff(structured_outputs: dict[str, Any]) -> bool:
    failed_statuses = {
        "directory_unavailable",
        "discovery_failed",
        "no_agent_found",
        "send_failed",
        "send_rejected",
        "unsupported_kind",
    }
    for output in structured_outputs.values():
        if not isinstance(output, dict):
            continue
        delivery_items: list[Any] = []
        delivery_result = output.get("delivery_result")
        if isinstance(delivery_result, dict):
            delivery_items.append(delivery_result)
        delivery_results = output.get("delivery_results")
        if isinstance(delivery_results, list):
            delivery_items.extend(item for item in delivery_results if isinstance(item, dict))
        for item in delivery_items:
            if str(item.get("status") or "").lower() in failed_statuses:
                return True
    return False


def _err(tool: str, message: str) -> dict[str, Any]:
    return {"tool": tool, "status": "error", "content": {"error": message}}


def _agent_summary(agent: DiscoveredAgent) -> dict[str, Any]:
    skills = sorted({s for cap in agent.skills for s in (cap.skill_tags + cap.custom_skill_tags)})
    return {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "organization": agent.organization,
        "description": agent.description,
        "skills": skills[:12],
        "skill_count": len(skills),
    }


def _agent_full_card(agent: DiscoveredAgent) -> dict[str, Any]:
    return {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "organization": agent.organization,
        "description": agent.description,
        "input_modes": agent.input_modes,
        "output_modes": agent.output_modes,
        "skills": [
            {
                "name": cap.name,
                "description": cap.description,
                "skill_ids": sorted({*cap.skill_tags, *cap.custom_skill_tags}),
                "examples": cap.examples,
                "input_modes": cap.input_modes,
                "output_modes": cap.output_modes,
                "risk_level": cap.risk_level.value,
                "action_type": cap.action_type,
            }
            for cap in agent.skills
        ],
    }


def _capability_for_skill(agent: DiscoveredAgent, skill: str) -> AgentCapability | None:
    for cap in agent.skills:
        if skill in (set(cap.skill_tags) | set(cap.custom_skill_tags) | {x.name for x in cap.official_oasf_skills}):
            return cap
    return None


def _canonical_skill(agent: DiscoveredAgent, skill: str) -> str | None:
    """Return the agent-advertised skill id matching ``skill``.

    If the requested string is already a fully-qualified skill id ("a/b") and
    is advertised, return it unchanged. Only when the requested string is an
    unscoped alias (e.g. "send" → "email/send") do we resolve to the first
    scoped tag on the matching capability.
    """
    for cap in agent.skills:
        all_aliases = set(cap.skill_tags) | set(cap.custom_skill_tags) | {x.name for x in cap.official_oasf_skills}
        if skill not in all_aliases:
            continue
        if "/" in skill:
            return skill
        for candidate in [*cap.skill_tags, *cap.custom_skill_tags]:
            if "/" in candidate:
                return candidate
        return skill
    return None


def _advertised_skills(agent: DiscoveredAgent) -> set[str]:
    out: set[str] = set()
    for cap in agent.skills:
        out.update(cap.skill_tags)
        out.update(cap.custom_skill_tags)
        out.update(x.name for x in cap.official_oasf_skills)
    return out


def _agent_has_email_skills(agent: DiscoveredAgent) -> bool:
    return any(skill.startswith("email/") for skill in _advertised_skills(agent))


def _request_fingerprint(
    *,
    agent_id: str,
    user_text: str,
    task_input: dict[str, Any],
    requested_skill: str | None,
) -> str:
    return json.dumps(
        {
            "agent_id": agent_id,
            "user_goal": _normalize(user_text),
            "task_input": task_input,
            "requested_skill": requested_skill or "",
        },
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )


def _chosen_skill_from_result(
    result: A2ATaskResult,
    *,
    requested_skill: str | None,
) -> str | None:
    output = result.structured_output or {}
    if isinstance(output, dict):
        for key in ("chosen_skill", "actual_skill", "selected_skill", "skill"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return requested_skill


def _last_text(result: A2ATaskResult) -> str:
    for message in reversed(result.messages):
        for part in message.parts:
            if part.kind == "text" and part.text:
                return part.text
    return result.error or ""


def _is_write_skill(skill: str) -> bool:
    lowered = skill.lower()
    return any(token in lowered for token in WRITE_SKILL_TOKENS)


def _is_email_skill(skill: str) -> bool:
    return skill.startswith("email/")


def _is_geospatial_agent(agent: DiscoveredAgent, skill: str | None) -> bool:
    advertised = _advertised_skills(agent)
    haystack = " ".join(
        [
            agent.agent_id,
            agent.name,
            agent.description,
            skill or "",
            *advertised,
        ]
    ).lower()
    return "geospatial" in haystack or "geo-spatial" in haystack


def _uploaded_files_from_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw = metadata.get("uploaded_files") or metadata.get("uploaded_images")  # back-compat
    if not isinstance(raw, list):
        return []
    files: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not _is_safe_uploaded_file(path):
            continue
        files.append(
            {
                "path": str(Path(path).resolve()),
                "filename": str(item.get("filename") or Path(path).name),
                "content_type": str(item.get("content_type") or "application/octet-stream"),
                "size": item.get("size"),
            }
        )
    return files


def _is_safe_uploaded_file(path_text: str) -> bool:
    if not path_text:
        return False
    try:
        path = Path(path_text).expanduser().resolve()
        upload_dir = Path(os.getenv("MAIN_AGENT_UPLOAD_DIR", "/tmp/planner_agent_uploads")).resolve()
        path.relative_to(upload_dir)
    except (OSError, ValueError):
        return False
    return path.is_file()


def _uploaded_file_to_a2a_part(file: dict[str, Any]) -> A2APart | None:
    path = Path(str(file.get("path") or ""))
    if not _is_safe_uploaded_file(str(path)):
        return None
    content_type = str(file.get("content_type") or "application/octet-stream")
    data = path.read_bytes()
    return A2APart(
        kind="file",
        content_type=content_type,
        bytes=base64.b64encode(data).decode("ascii"),
        metadata={
            "name": str(file.get("filename") or path.name),
            "transfer": "a2a_file_bytes",
        },
    )


def _uploaded_file_summary(file: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": file.get("filename"),
        "content_type": file.get("content_type"),
        "size": file.get("size"),
    }


def _salvage_final_text(state: TurnState, structured_outputs: dict[str, Any]) -> str:
    """Return a usable response when the LLM emitted an empty final_answer.

    Walk back through the tool observations: prefer the most recent
    call_agent's `text` (the agent's user-facing message), then
    structured_output's summary/response, then any non-empty text seen.
    """
    for obs in reversed(state.observations):
        if obs.get("tool") != "call_agent":
            continue
        content = obs.get("content") or {}
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        so = content.get("structured_output") or {}
        if isinstance(so, dict):
            for key in ("response_text", "summary", "final_text"):
                value = so.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            response = so.get("response")
            if isinstance(response, dict):
                for key in ("summary", "text", "plan"):
                    value = response.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    for value in reversed(list(structured_outputs.values())):
        if isinstance(value, dict):
            for key in ("response_text", "summary", "final_text"):
                cand = value.get(key)
                if isinstance(cand, str) and cand.strip():
                    return cand.strip()
    return "(no answer produced)"


_CONFUSED_PHRASES = (
    "i'm not sure how to answer that",
    "i am not sure how to answer that",
    "i don't know how to answer that",
    "i do not know how to answer that",
)


def _is_confused_response(text: str) -> bool:
    lowered = text.lower().strip()
    return any(lowered.startswith(phrase) or lowered == phrase for phrase in _CONFUSED_PHRASES)


_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


def _augment_email_task_input(
    *,
    task_input: dict[str, Any],
    user_text: str,
    conversation_history: list[str],
) -> dict[str, Any]:
    """Fill missing email task_input fields from natural-language hints.

    The Email Agent expects structured fields (to/subject/body). Some LLMs
    (especially newer ones) put the info in the free-text prompt and leave
    task_input empty. We rescue them by parsing the user text.
    """
    augmented = dict(task_input)

    # to
    if not augmented.get("to"):
        addrs = list(dict.fromkeys(_EMAIL_RE.findall(user_text)))
        if not addrs:
            for entry in reversed(conversation_history):
                addrs = list(dict.fromkeys(_EMAIL_RE.findall(entry)))
                if addrs:
                    break
        if addrs:
            augmented["to"] = addrs

    # subject
    if not augmented.get("subject"):
        subject = _quoted_value_after(user_text, ("subject", "title", "topic"))
        if subject:
            augmented["subject"] = subject

    # body
    if not augmented.get("body"):
        body = _quoted_value_after(user_text, ("body", "content", "message", "say"))
        if body:
            augmented["body"] = body

    # If the user references a prior code block with phrases like "this program",
    # pull the latest fenced code block from the assistant's prior turns.
    if not augmented.get("body") and _references_prior_artifact(user_text):
        prior = _latest_fenced_block(conversation_history)
        if prior:
            augmented["body"] = prior

    return augmented


def _quoted_value_after(text: str, names: tuple[str, ...]) -> str | None:
    quoted = r'"([^"]+)"|\'([^\']+)\'|“([^”]+)”|‘([^’]+)’'
    for name in names:
        m = re.search(rf"\b{re.escape(name)}\s*(?:is|=|:)?\s*(?:{quoted})", text, re.IGNORECASE)
        if m:
            for g in m.groups():
                if g:
                    return g.strip()
    return None


def _references_prior_artifact(text: str) -> bool:
    lowered = text.lower()
    return any(t in lowered for t in ("this program", "the program", "this code", "the code", "this plan", "the plan"))


def _latest_fenced_block(history: list[str]) -> str | None:
    pattern = re.compile(r"```(?:[A-Za-z0-9_+-]*)\n(.*?)```", re.DOTALL)
    for entry in reversed(history):
        matches = pattern.findall(entry)
        if matches:
            block = matches[-1].strip()
            if block:
                return block
    return None
