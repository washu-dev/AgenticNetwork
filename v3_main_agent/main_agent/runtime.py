from __future__ import annotations

import logging
import re
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.common.models import (
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
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.directory.backends import DirectoryUnavailable
from v2_agent_network.services.state.store import InMemoryStateStore

from v3_main_agent.main_agent.config import MainAgentConfig
from v3_main_agent.main_agent.llm_client import LLMClient, ToolCall, TurnState

logger = logging.getLogger(__name__)


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
        state = TurnState(
            context_id=context.context_id,
            user_text=text,
            conversation_history=list(context.conversation_history)[-self.config.history_limit :],
            pending_confirmation=self.store.get_pending_confirmation(context.context_id),
        )

        result = await self._run_loop(state=state, context=context)
        # Append assistant final text to conversation history.
        if result.text and result.state in {A2ATaskState.completed, A2ATaskState.input_required}:
            entry = f"assistant: {result.text}"
            if not context.conversation_history or context.conversation_history[-1] != entry:
                context.conversation_history.append(entry)
            context.updated_at = utc_now()
            self.store.save_context(context)
        return result

    # -------- core loop --------

    async def _run_loop(self, *, state: TurnState, context: PIContext) -> TurnResult:
        tool_trace: list[dict[str, Any]] = []
        selected_agents: list[dict[str, Any]] = []
        structured_outputs: dict[str, Any] = {}
        # (agent_id, skill) tuples for calls that completed successfully in this
        # turn. Only successful ones gate dedup so transient A2A failures can be
        # retried by the LLM.
        successful_calls: set[tuple[str, str]] = set()
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
                tool_trace.append(
                    {
                        "tool": "reasoning",
                        "status": "ok",
                        "content": {"text": tool_call.reasoning, "next_tool": tool_call.tool_name},
                    }
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
        successful_calls: set[tuple[str, str]],
        discovered: dict[str, DiscoveredAgent],
    ) -> TurnResult | None:
        name = tool_call.tool_name
        args = dict(tool_call.arguments or {})

        if name == "final_answer":
            text = str(args.get("text") or "").strip()
            if not text:
                # Salvage: if the LLM ran out of output tokens (common on reasoning
                # models with large tool observations) it may emit final_answer({}).
                # Fall back to the last call_agent text so the user still sees
                # the agent's actual output instead of "(empty)".
                text = _salvage_final_text(state, structured_outputs)
            tool_trace.append({"tool": name, "status": "ok", "args": args})
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
            tool_trace.append({"tool": name, "status": "ok", "args": args})
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
                discovered=discovered,
                selected_agents=selected_agents,
                structured_outputs=structured_outputs,
                successful_calls=successful_calls,
            )
            tool_trace.append({"tool": name, "status": obs["status"], "content": obs["content"]})
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

        tool_trace.append(obs)
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
        discovered: dict[str, DiscoveredAgent],
        selected_agents: list[dict[str, Any]],
        structured_outputs: dict[str, Any],
        successful_calls: set[tuple[str, str]],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        agent_id = str(args.get("agent_id") or "").strip()
        skill = str(args.get("skill") or "").strip()
        task_input = args.get("task_input") if isinstance(args.get("task_input"), dict) else {}

        if not agent_id or not skill:
            return _err("call_agent", "Both agent_id and skill are required."), None
        if "://" in agent_id or "://" in skill:
            return _err("call_agent", "URLs are not accepted; use discovered agent_id and skill."), None

        logger.info(
            "main_agent_call_agent_args context_id=%s agent_id=%s skill=%s task_input_keys=%s",
            context.context_id,
            agent_id,
            skill,
            sorted(task_input.keys()) if task_input else [],
        )

        # Belt-and-braces: if the LLM forgot to fill task_input for an email-like skill,
        # extract recipient/subject/body from the user's text and conversation history
        # so the email agent doesn't bounce on "missing recipient address".
        if _is_email_skill(skill):
            task_input = _augment_email_task_input(
                task_input=task_input,
                user_text=state.user_text,
                conversation_history=context.conversation_history,
            )

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

        canonical_skill = _canonical_skill(agent, skill) or skill
        capability = _capability_for_skill(agent, canonical_skill)
        if capability is None:
            return (
                _err(
                    "call_agent",
                    f"Skill {skill!r} is not advertised by agent {agent_id!r}. "
                    f"Available: {sorted(_advertised_skills(agent))}",
                ),
                None,
            )

        # Deduplicate: same (agent, skill) already completed *successfully* in
        # this turn. Failed calls (a2a_call_failed, etc.) intentionally skip
        # this gate so the LLM can retry transient errors.
        if (agent.agent_id, canonical_skill) in successful_calls:
            prior_task = next(
                (
                    step["task_id"]
                    for step in selected_agents
                    if step.get("agent_id") == agent.agent_id
                    and step.get("skill") == canonical_skill
                    and step.get("task_id")
                ),
                None,
            )
            return (
                {
                    "tool": "call_agent",
                    "status": "skipped",
                    "content": {
                        "skill": canonical_skill,
                        "agent_id": agent.agent_id,
                        "reason": (
                            f"{canonical_skill} already ran successfully on {agent.agent_id} "
                            "in this turn. The previous structured_output is in your context — "
                            "do NOT call it again; call final_answer with the result."
                        ),
                        "previous_task_id": prior_task,
                    },
                },
                None,
            )

        decision = self._evaluate_policy(canonical_skill, capability)
        if decision.status == PolicyDecisionStatus.requires_confirmation:
            pending = {
                "skill": canonical_skill,
                "agent_id": agent.agent_id,
                "task_input": task_input,
                "reason": decision.reason,
                "policy_decision": decision.status.value,
                "confirmation_status": ConfirmationStatus.pending.value,
                "selected_agent": agent.model_dump(mode="json"),
            }
            self.store.save_pending_confirmation(context.context_id, pending)
            selected_agents.append({"skill": canonical_skill, "agent_id": agent.agent_id, "task_id": None})
            obs = {
                "tool": "call_agent",
                "status": "confirmation_required",
                "content": {
                    "skill": canonical_skill,
                    "agent_id": agent.agent_id,
                    "reason": decision.reason,
                    "task_input": task_input,
                },
            }
            terminal_payload = {
                "text": (
                    f"This action ({canonical_skill}) needs your confirmation. "
                    f"Reply 'yes' to proceed, 'no' to cancel."
                ),
                "pending_confirmation": pending,
            }
            return obs, terminal_payload

        # Allowed → invoke A2A.
        request = self._build_a2a_request(
            context=context,
            agent=agent,
            skill=canonical_skill,
            task_input=task_input,
            user_text=state.user_text,
            confirmed=False,
        )
        result = await self._send_task(agent, request)
        self._save_invocation(context, agent, canonical_skill, result, decision)
        selected_agents.append(
            {"skill": canonical_skill, "agent_id": agent.agent_id, "task_id": result.task_id}
        )
        if result.structured_output:
            structured_outputs[canonical_skill] = result.structured_output
        if result.state == A2ATaskState.completed:
            successful_calls.add((agent.agent_id, canonical_skill))

        obs = {
            "tool": "call_agent",
            "status": result.state.value,
            "content": {
                "skill": canonical_skill,
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
        if context_id:
            try:
                context = self.store.get_context(context_id)
            except KeyError:
                context = self._new_context(user_id, channel, text)
            else:
                context.current_goal = text
                context.updated_at = utc_now()
                context.conversation_history.append(f"user: {text}")
                return context
        else:
            context = self._new_context(user_id, channel, text)
        self.store.save_context(context)
        return context

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

    def _evaluate_policy(self, skill: str, capability: AgentCapability) -> PolicyDecision:
        if self.config.policy_mode in {"permissive", "off", "disabled"}:
            return PolicyDecision(
                status=PolicyDecisionStatus.allowed,
                reason=f"{skill} allowed in permissive mode.",
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
        skill: str,
        task_input: dict[str, Any],
        user_text: str,
        confirmed: bool,
    ) -> A2ATaskRequest:
        metadata: dict[str, Any] = {
            "user_id": context.user_id,
            "channel": context.channel.value,
            "skill": skill,
            **{k: v for k, v in task_input.items() if k not in {"user_id", "channel", "skill"}},
        }
        if confirmed:
            metadata["confirmed"] = True
            metadata["confirmation_status"] = ConfirmationStatus.confirmed.value
        parts = [A2APart(kind="text", text=user_text)]
        if task_input:
            parts.append(A2APart(kind="data", data=dict(task_input)))
        return A2ATaskRequest(
            task_id=str(uuid4()),
            context_id=context.context_id,
            target_agent_id=agent.agent_id,
            skill=skill,
            user_goal=user_text,
            messages=[A2AMessage(role="user", parts=parts)],
            metadata=metadata,
        )

    async def _send_task(self, agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult:
        try:
            return await self.a2a_client.send_task(agent, request)
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


def _normalize_agent_key(value: str) -> str:
    """Lowercase + strip non-alphanumerics so that 'Disaster Response Agent',
    'disaster-response-agent', 'disaster_response_agent', and 'DisasterResponseAgent'
    all collapse to the same key."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


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
