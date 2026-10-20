from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import logging
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from uuid import uuid4

from pydantic import BaseModel, Field

from agentnet.a2a.client import A2AClient
from agentnet.directory import DirectoryUnavailable
from agentnet.directory.client import DirectoryClient
from chat_app.planner.models import (
    A2AMessage,
    A2APart,
    A2ATaskRequest,
    A2ATaskResult,
    A2ATaskState,
    AgentCapability,
    Channel,
    ConfirmationStatus,
    ConversationContext,
    DiscoveredAgent,
    PolicyDecision,
    PolicyDecisionStatus,
    utc_now,
)
from chat_app.planner.memory_store import MemoryStore

from chat_app.env import upload_dir
from chat_app.planner.config import PlannerConfig
from chat_app.planner.credential_gate import CredentialGate
from chat_app.planner.llm_client import LLMClient, ToolCall, TurnState
# The store modules themselves, not names from them: tests patch their attributes.
from chat_app.store import conversation_store, pending_store

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


class ContextOwnershipError(Exception):
    """Raised when a request presents a context_id owned by a different user.

    Multi-user hardening (H1): the working context is bound to its creator's
    user_id; a caller may not read or append to another user's context. The
    server maps this to HTTP 403.
    """

    def __init__(self, context_id: str) -> None:
        super().__init__(f"context {context_id} is not owned by the requester")
        self.context_id = context_id


class PendingStoreUnavailable(Exception):
    """Raised when the database is configured but a pending action can't be claimed from it."""


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


class PlannerRuntime:
    """The planner: an LLM tool loop that picks agents and calls them.

    Owns ADS discovery, A2A transport, conversation memory, and policy gating.
    The LLM only sees TurnState (safe view) and chooses one tool per step.
    """

    def __init__(
        self,
        *,
        config: PlannerConfig,
        directory_client: DirectoryClient,
        a2a_client: A2AClient,
        state_store: MemoryStore,
        llm_client: LLMClient,
        credential_gate: "CredentialGate | None" = None,
    ):
        self.config = config
        self.directory = directory_client
        self.a2a_client = a2a_client
        self.store = state_store
        self.llm_client = llm_client
        # P4c: per-agent credential grants. None (no DB configured) → grants can't be
        # checked, so only declared types are injected; see _scope_credentials.
        self.credential_gate = credential_gate
        # context_ids with an answer to their pending action running in this process;
        # another answer meanwhile gets answer_in_progress (see _answer_pending).
        self._answering: set[str] = set()
        # Claim cleanups (deletes, releases) still running after their answer was cancelled,
        # held so they finish (see _run_to_end).
        self._cleanups: set[asyncio.Task] = set()

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
        if context_id and (self._is_affirmative(text) or self._is_negative(text)):
            # Also while an earlier answer is still running (a double send, a retry, a reply
            # on another process): with a DB its row stays, claimed, until it is done, and
            # this process's own answers are in _answering (checked after the lookup, which
            # yields). This one then gets answer_in_progress instead of an LLM turn on "yes".
            pending, _ = await self._lookup_pending(context_id)
            if pending is not None or context_id in self._answering:
                # H1: a yes or no acts on the pending action with the caller's
                # credentials and identity, so the caller must own the conversation,
                # checked exactly as _open_context checks every other message.
                await self._owned_context(user_id, channel, context_id)
                return await self._answer_pending(
                    context_id, channel, metadata or {},
                    confirmed=not self._is_negative(text), user_text=text,
                )

        context = await self._open_context(user_id, channel, text, context_id)
        uploaded_files = _uploaded_files_from_metadata(metadata or {})
        credentials = (metadata or {}).get("credentials") or {}
        auth_user_id = (metadata or {}).get("auth_user_id")
        state = TurnState(
            context_id=context.context_id,
            user_text=text,
            conversation_history=list(context.conversation_history)[-self.config.history_limit :],
            pending_confirmation=await self._get_pending(context.context_id),
            uploaded_files=uploaded_files,
        )

        result = await self._run_loop(
            state=state,
            context=context,
            credentials=credentials,
            auth_user_id=auth_user_id,
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
        context: ConversationContext,
        credentials: dict[str, Any] | None = None,
        auth_user_id: str | None = None,
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
                logger.exception("planner_llm_failed context_id=%s", context.context_id)
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
                "planner_step context_id=%s step=%d tool=%s",
                context.context_id,
                step,
                tool_call.tool_name,
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
                auth_user_id=auth_user_id,
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
        context: ConversationContext,
        tool_trace: list[dict[str, Any]],
        selected_agents: list[dict[str, Any]],
        structured_outputs: dict[str, Any],
        successful_calls: set[str],
        discovered: dict[str, DiscoveredAgent],
        credentials: dict[str, Any] | None = None,
        auth_user_id: str | None = None,
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
                auth_user_id=auth_user_id,
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
                # Terminal payload from call_agent: either a confirmation/consent
                # prompt (carries pending_confirmation) or a hard block (e.g. a
                # required credential the user hasn't added — no pending state).
                # Build the TurnResult HERE so it carries the full tool_trace.
                pending_confirmation = pending.get("pending_confirmation")
                return TurnResult(
                    context_id=context.context_id,
                    state=A2ATaskState.input_required,
                    text=pending["text"],
                    tool_trace=list(tool_trace),
                    selected_agents=list(selected_agents),
                    confirmation_required=pending_confirmation is not None,
                    pending_confirmation=pending_confirmation,
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
        context: ConversationContext,
        tool_trace: list[dict[str, Any]],
        discovered: dict[str, DiscoveredAgent],
        selected_agents: list[dict[str, Any]],
        structured_outputs: dict[str, Any],
        successful_calls: set[str],
        credentials: dict[str, Any] | None = None,
        auth_user_id: str | None = None,
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
            "planner_call_agent_args context_id=%s agent_id=%s requested_skill=%s task_input_keys=%s",
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
                "planner_skill_hint_not_advertised context_id=%s agent_id=%s requested_skill=%s advertised=%s",
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
                "planner_followup_dispatch_after_failed_peer_handoff context_id=%s prior_agent_id=%s next_agent_id=%s",
                context.context_id,
                prior_dispatch.get("agent_id"),
                agent.agent_id,
            )

        # P4c: credential gate — runs before policy/dispatch. Blocks or asks for
        # consent when the agent declares credentials the user hasn't provided/granted;
        # otherwise returns the least-privilege subset to inject.
        scoped_credentials, cred_terminal = await self._enforce_credentials(
            context=context,
            agent=agent,
            credentials=credentials,
            auth_user_id=auth_user_id,
            canonical_hint=canonical_hint,
            requested_skill=requested_skill,
            task_input=task_input,
        )
        if cred_terminal is not None:
            obs, payload = cred_terminal
            selected_agents.append(
                {
                    "skill": canonical_hint,
                    "requested_skill": requested_skill,
                    "chosen_skill": None,
                    "agent_id": agent.agent_id,
                    "task_id": None,
                    "request_fingerprint": fingerprint,
                    "state": obs["status"],
                }
            )
            return obs, payload

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
                # The request the owner approves; the resumed call sends this, not
                # whatever current_goal a later message has set by then.
                "user_goal": context.current_goal,
                # Names this prompt, so the confirm button answers the prompt it was shown on.
                "prompt_id": uuid4().hex,
            }
            await self._save_pending(context.context_id, context.user_id, pending)
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
            credentials=scoped_credentials,
        )
        remote_progress_callback = None
        if progress_callback is not None:
            async def remote_progress_callback(entry: dict[str, Any]) -> None:
                await _record_trace(tool_trace, entry, progress_callback)

        result = await self._send_task(agent, request, progress_callback=remote_progress_callback)
        chosen_skill = _chosen_skill_from_result(result, requested_skill=canonical_hint or requested_skill)
        record_skill = chosen_skill or canonical_hint or requested_skill or "agent/autonomous"
        self.store.save_context(context)
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

    async def _answer_pending(
        self,
        context_id: str,
        channel: Channel,
        metadata: dict[str, Any],
        *,
        confirmed: bool,
        user_text: str | None = None,
        prompt_id: str | None = None,
    ) -> TurnResult:
        """Act on the owner's answer to a pending write confirmation or credential consent.

        The one entry point for a chat "yes"/"no" and the confirm button, called after the
        ownership check. Answers can arrive together: a double click, a client retry, a chat
        "yes" racing the button, replies on two processes. Each one used to read the action,
        call the agent and clear the action afterwards, so both ran it. Now:

        - Within this process one answer per conversation runs at a time; another gets
          ``answer_in_progress`` and leaves the action alone.
        - Each handler does everything that can fail first (credential lookups), then claims
          the action (_claim_this) and calls the agent with nothing that can fail in between,
          so an answer either leaves the action pending or acts on it, never half of each.
          Across processes the claim decides: the loser gets ``no_pending``, and an answer
          arriving while the claimed action runs gets ``answer_in_progress``.
        - ``prompt_id`` (the confirm button sends the one it was shown with) must name the
          pending prompt; a press on a prompt a newer one replaced gets ``superseded``.
        """
        if context_id in self._answering:
            return self._in_progress(context_id)
        self._answering.add(context_id)
        try:
            user_text = user_text or ("yes" if confirmed else "no")
            if confirmed and context_id not in self.store.contexts:
                return self._unknown_context(context_id)
            pending, claimed = await self._lookup_pending(context_id)
            if pending is None:
                return self._no_pending(context_id)
            if prompt_id is not None and pending.get("prompt_id") != prompt_id:
                return self._superseded(context_id)
            if claimed:
                return self._in_progress(context_id)
            if not confirmed:
                return await self._cancel_confirmation(context_id, pending, user_text=user_text)
            if pending.get("kind") == "credential_consent":
                return await self._resume_credential_consent(
                    context_id, channel, metadata, pending, user_text=user_text,
                )
            return await self._resume_confirmation(
                context_id, channel, metadata, pending, user_text=user_text,
            )
        finally:
            self._answering.discard(context_id)

    async def _claim_this(self, context_id: str, pending: dict[str, Any]) -> tuple[str, TurnResult | None]:
        """Claim the pending action this answer read: (claim id, None) when claimed, which the
        handler then acts inside _claimed (or passes to _finish_pending). Otherwise ("", the
        reply to give), and nothing is acted on: another answer got there first, a newer
        prompt had replaced this one (it stays pending), or the database couldn't serve the
        claim (one it may have applied is released)."""
        try:
            claim_id = await self._claim_pending(context_id, pending)
        except PendingStoreUnavailable:
            return "", TurnResult(
                context_id=context_id,
                state=A2ATaskState.failed,
                text="Your answer couldn't be recorded, so nothing was run or cancelled. Try again in a moment.",
                errors=["pending_store_unavailable"],
                llm_model=self.config.llm_model,
            )
        if claim_id is None:
            return "", self._no_pending(context_id)
        return claim_id, None

    def _in_progress(self, context_id: str) -> TurnResult:
        return TurnResult(
            context_id=context_id,
            state=A2ATaskState.failed,
            text="Your earlier answer to this is still being handled.",
            errors=["answer_in_progress"],
            llm_model=self.config.llm_model,
        )

    def _superseded(self, context_id: str) -> TurnResult:
        return TurnResult(
            context_id=context_id,
            state=A2ATaskState.failed,
            text="That prompt was replaced by a newer one, so nothing was run or cancelled. Answer the newest prompt.",
            errors=["superseded"],
            llm_model=self.config.llm_model,
        )

    def _no_pending(self, context_id: str) -> TurnResult:
        return TurnResult(
            context_id=context_id,
            state=A2ATaskState.failed,
            text="Nothing is waiting for your answer here; it may already have been confirmed or cancelled.",
            errors=["no_pending"],
            llm_model=self.config.llm_model,
        )

    def _unknown_context(self, context_id: str) -> TurnResult:
        return TurnResult(
            context_id=context_id,
            state=A2ATaskState.failed,
            text=f"Unknown context_id {context_id}.",
            errors=["unknown_context"],
            llm_model=self.config.llm_model,
        )

    # The three handlers below are called by _answer_pending with the pending action it
    # read; each claims it with _claim_this right before acting and holds the claim while
    # it acts (_claimed), or finishes it at once (a cancel).

    async def _resume_confirmation(
        self,
        context_id: str,
        channel: Channel,
        metadata: dict[str, Any],
        pending: dict[str, Any],
        user_text: str = "yes",
    ) -> TurnResult:
        try:
            context = self.store.get_context(context_id)
        except KeyError:
            return self._unknown_context(context_id)
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

        # P4c: apply the same least-privilege scoping on the post-confirmation path.
        scoped = await self._scope_credentials(
            agent, metadata.get("credentials") or {}, metadata.get("auth_user_id")
        )
        request = self._build_a2a_request(
            context=context,
            agent=agent,
            skill=skill,
            task_input=task_input,
            user_text=pending.get("user_goal") or context.current_goal,
            confirmed=True,
            uploaded_files=[],
            credentials=scoped,
        )
        claim_id, refused = await self._claim_this(context_id, pending)
        if refused is not None:
            return refused
        # Record the user's confirmation turn so future LLM turns can see it.
        context.conversation_history.append(f"user: {user_text}")
        context.updated_at = utc_now()
        async with self._claimed(context_id, claim_id):
            result = await self._send_task(agent, request)
        self.store.save_context(context)

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

    async def _cancel_confirmation(
        self,
        context_id: str,
        pending: dict[str, Any],
        user_text: str = "no",
    ) -> TurnResult:
        claim_id, refused = await self._claim_this(context_id, pending)
        if refused is not None:
            return refused
        await self._finish_pending(context_id, claim_id)
        skill = str(pending.get("skill") or "pending_action")
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

    # -------- credentials (P4c) --------

    async def _scope_credentials(
        self,
        agent: DiscoveredAgent,
        credentials: dict[str, Any] | None,
        auth_user_id: str | None,
        consented: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Least-privilege filter: only creds this agent DECLARED and the user GRANTED.

        An agent that declares none gets none. Without a gate/identity grants can't be
        checked, so only the declared types pass. ``consented`` types count as granted
        (a consent being answered now, recorded after the call). The one exception is a
        legacy agent named in ``PLANNER_UNSCOPED_CREDENTIAL_AGENTS`` (see :meth:`_unscoped_legacy`).
        """
        creds = credentials or {}
        if self._unscoped_legacy(agent, creds):
            return creds
        declared = {r.type: creds[r.type] for r in agent.required_credentials if r.type in creds}
        if not declared or self.credential_gate is None or not auth_user_id:
            return declared
        granted = set(await self.credential_gate.list_granted(auth_user_id, agent.agent_id)) | set(consented)
        return {t: v for t, v in declared.items() if t in granted}

    def _unscoped_legacy(self, agent: DiscoveredAgent, creds: dict[str, Any]) -> bool:
        """True if this agent gets the whole bundle: an opt-in for agents that read
        credentials (the email agent's ``google_oauth``) without declaring them."""
        if agent.agent_id not in self.config.unscoped_credential_agents:
            return False
        if creds:
            logger.warning(
                "credentials_unscoped_legacy_agent agent_id=%s types=%s",
                agent.agent_id, ",".join(sorted(creds)),
            )
        return True

    async def _enforce_credentials(
        self,
        *,
        context: ConversationContext,
        agent: DiscoveredAgent,
        credentials: dict[str, Any] | None,
        auth_user_id: str | None,
        canonical_hint: str | None,
        requested_skill: str | None,
        task_input: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], dict[str, Any]] | None]:
        """Gate a call on the agent's declared credentials.

        Returns ``(scoped_credentials, None)`` to proceed, or
        ``(None-ish, (obs, terminal_payload))`` to short-circuit with a block or a
        consent prompt. Optional creds never block; they are injected only once
        granted. With nothing declared, or no gate/identity, see :meth:`_scope_credentials`.
        """
        creds = credentials or {}
        reqs = agent.required_credentials
        if not reqs or self.credential_gate is None or not auth_user_id:
            return await self._scope_credentials(agent, creds, auth_user_id), None

        granted = await self.credential_gate.list_granted(auth_user_id, agent.agent_id)
        scoped: dict[str, Any] = {}
        missing: list[Any] = []    # required, not stored → block
        ungranted: list[Any] = []  # required, stored, not granted → consent
        for r in reqs:
            if r.type in creds and r.type in granted:
                scoped[r.type] = creds[r.type]
            elif r.required and r.type not in creds:
                missing.append(r)
            elif r.required:
                ungranted.append(r)
            # optional + (missing or ungranted) → skip silently

        if missing:
            labels = ", ".join(f"{r.label} ({r.type})" for r in missing)
            obs = {
                "tool": "call_agent",
                "status": "credentials_required",
                "content": {
                    "agent_id": agent.agent_id,
                    "reason": "missing_credentials",
                    "missing_credentials": [r.model_dump() for r in missing],
                },
            }
            payload = {
                "text": (
                    f"“{agent.name}” needs {labels}, which you haven't added yet. "
                    "Add it under Settings → Credentials, then ask me again."
                )
            }
            return {}, (obs, payload)

        if ungranted:
            pending = {
                "kind": "credential_consent",
                "agent_id": agent.agent_id,
                "grant_types": [r.type for r in ungranted],
                "skill": canonical_hint,
                "requested_skill": requested_skill,
                "task_input": task_input,
                "confirmation_status": ConfirmationStatus.pending.value,
                "selected_agent": agent.model_dump(mode="json"),
                "user_goal": context.current_goal,
                "prompt_id": uuid4().hex,
            }
            await self._save_pending(context.context_id, context.user_id, pending)
            labels = ", ".join(f"{r.label} ({r.type})" for r in ungranted)
            obs = {
                "tool": "call_agent",
                "status": "credential_consent_required",
                "content": {
                    "agent_id": agent.agent_id,
                    "reason": "credential_consent",
                    "grant_credentials": [r.model_dump() for r in ungranted],
                },
            }
            payload = {
                "text": (
                    f"“{agent.name}” wants to use your {labels}. "
                    "Reply 'yes' to allow it (you can revoke anytime in Settings), or 'no' to cancel."
                ),
                "pending_confirmation": pending,
            }
            return {}, (obs, payload)

        return (creds if self._unscoped_legacy(agent, creds) else scoped), None

    async def _resume_credential_consent(
        self,
        context_id: str,
        channel: Channel,
        metadata: dict[str, Any],
        pending: dict[str, Any],
        user_text: str = "yes",
    ) -> TurnResult:
        """User approved a credential grant → dispatch the call, then persist the grants.

        Symmetric to :meth:`_resume_confirmation`. Note: approving the credential
        also proceeds with the call (we do not additionally re-prompt the write
        policy — credential consent implies proceed; see docs/AUTH.md §4, The flow, step 3).
        """
        try:
            context = self.store.get_context(context_id)
        except KeyError:
            return self._unknown_context(context_id)

        agent = DiscoveredAgent.model_validate(pending["selected_agent"])
        auth_user_id = metadata.get("auth_user_id")
        grant_types = list(pending.get("grant_types") or [])
        credentials = metadata.get("credentials") or {}
        # The consent given now covers this call before it is recorded below.
        scoped = await self._scope_credentials(agent, credentials, auth_user_id, consented=grant_types)
        skill = pending.get("skill") or pending.get("requested_skill")
        task_input = dict(pending.get("task_input") or {})
        request = self._build_a2a_request(
            context=context,
            agent=agent,
            skill=skill,
            task_input=task_input,
            user_text=pending.get("user_goal") or context.current_goal,
            confirmed=False,
            uploaded_files=[],
            credentials=scoped,
        )
        claim_id, refused = await self._claim_this(context_id, pending)
        if refused is not None:
            return refused
        context.conversation_history.append(f"user: {user_text}")
        context.updated_at = utc_now()
        async with self._claimed(context_id, claim_id):
            result = await self._send_task(agent, request)
            self.store.save_context(context)
            # Recorded after the call, so nothing that can fail sits between the claim and
            # the call. If recording fails, the call has still run on the consent just given
            # and the owner is asked again next time.
            if self.credential_gate is not None and auth_user_id:
                try:
                    for cred_type in grant_types:
                        await self.credential_gate.add_grant(auth_user_id, agent.agent_id, cred_type)
                except Exception:
                    logger.warning("credential_grant_record_failed agent_id=%s", agent.agent_id, exc_info=True)

        text = _last_text(result) or f"{skill or 'The request'} completed."
        context.conversation_history.append(f"assistant: {text}")
        context.updated_at = utc_now()
        self.store.save_context(context)
        return TurnResult(
            context_id=context_id,
            state=result.state,
            text=text,
            tool_trace=[{
                "tool": "call_agent",
                "status": result.state.value,
                "content": {
                    "skill": skill, "agent_id": agent.agent_id, "task_id": result.task_id,
                    "granted_credentials": grant_types,
                    "structured_output": result.structured_output,
                },
            }],
            selected_agents=[{"skill": skill, "agent_id": agent.agent_id, "task_id": result.task_id}],
            structured_outputs=({skill: result.structured_output} if (skill and result.structured_output) else {}),
            llm_model=self.config.llm_model,
        )

    # -------- helpers --------

    async def _open_context(
        self,
        user_id: str,
        channel: Channel,
        text: str,
        context_id: str | None,
    ) -> ConversationContext:
        # H1: honor the caller-supplied context_id and bind each context to its
        # owner. No global slot — a missing/unknown id starts a fresh, owned context.
        existing = await self._owned_context(user_id, channel, context_id) if context_id else None
        if existing is not None:
            existing.channel = channel
            existing.current_goal = text
            existing.updated_at = utc_now()
            existing.conversation_history.append(f"user: {text}")
            return self.store.save_context(existing)

        # Unknown id (never persisted) or none: create a fresh context. Reuse the
        # supplied id when present so the browser's session stays continuous.
        context = self._new_context(user_id, channel, text, context_id=context_id)
        return self.store.save_context(context)

    async def _owned_context(self, user_id: str, channel: Channel, context_id: str) -> ConversationContext | None:
        """Return the existing context for ``context_id``, or None if none is recorded.

        Raises ContextOwnershipError (HTTP 403) when it belongs to another user. The
        owner is the in-memory context's; else, H2, the persisted conversation's,
        rehydrated so a conversation survives a restart or lands on a different planner
        process; else, H2b, the persisted pending action's, which is written during the
        turn and so can exist before (or without) the conversation it belongs to. An id
        with no owner anywhere is unclaimed, and _open_context starts a fresh context.
        """
        existing = self.store.contexts.get(context_id)
        if existing is None:
            existing = await self._rehydrate_context(context_id, channel)
        owner = existing.user_id if existing is not None else await pending_store.owner(context_id)
        if owner is not None and owner != user_id:
            raise ContextOwnershipError(context_id)
        return existing

    # H2b: pending confirmations are write-through cached in the in-memory store and
    # persisted to Postgres, so a prompt raised on one process can be resumed on another.
    # The Postgres calls are best-effort: if the DB is unreachable, pending state
    # degrades to the process-local cache rather than failing the turn, except an
    # answer's claim, which is refused instead (see _claim_pending). When it is
    # reachable, the row is read on every lookup (see _lookup_pending), and an answer
    # marks it claimed before acting (see _claim_pending), keeps the claim fresh while
    # it acts and deletes the row after (see _claimed).
    async def _save_pending(self, context_id: str, owner_email: str, pending: dict) -> None:
        self.store.save_pending_confirmation(context_id, pending)
        try:
            await pending_store.save(context_id, owner_email, pending)
        except Exception:
            logger.warning("pending_confirmation_persist_failed", exc_info=True)

    async def _get_pending(self, context_id: str) -> dict | None:
        """The pending action waiting for an answer, or None (none, or one being answered)."""
        pending, claimed = await self._lookup_pending(context_id)
        return None if claimed else pending

    async def _lookup_pending(self, context_id: str) -> tuple[dict | None, bool]:
        """(pending action, claimed): claimed means an answer on some process is acting
        on it now. Without a DB a claim removes the in-memory copy, so claimed is always
        False and this process's answer in progress is in ``self._answering``."""
        cached = self.store.get_pending_confirmation(context_id)
        if not pending_store.enabled():
            return cached, False
        try:
            row = await pending_store.get(context_id)
        except Exception:
            logger.warning("pending_confirmation_load_failed", exc_info=True)
            return cached, False
        # With a DB the row wins over this process's copy: another process may have
        # resumed or cancelled the action since it was cached here, and acting on the
        # stale copy would run a confirmed write a second time.
        if row is None:
            self.store.clear_pending_confirmation(context_id)
            return None, False
        pending, claimed = row
        if claimed:
            self.store.clear_pending_confirmation(context_id)
        else:
            self.store.save_pending_confirmation(context_id, pending)  # warm the cache
        return pending, claimed

    async def _claim_pending(self, context_id: str, pending: dict) -> str | None:
        """Claim ``pending`` (the action this answer read), so only one answer acts on it.

        Returns the claim's id, or None if another answer or a newer prompt got there first.
        Two answers can arrive together: a double click, a client retry, a chat "yes" and
        the button, or replies on two processes. When each read the action and cleared it
        only after the agent call, both ran it. Now an answer runs only an action it
        claimed, and only if it is still the one it read. With a DB the row decides (one
        UPDATE marking it claimed under this answer's id), and this process's copy is
        dropped. If that statement fails, the answer is refused (PendingStoreUnavailable):
        running the cached copy would leave the row for a later answer, here or on another
        process, to run again. A claim that fails or is cancelled (a closed chat stream) may
        have been applied, or be applied later, and nothing will act on it, so it is
        released by its id, which can't touch another answer's claim and makes a later
        application take nothing. Without a DB the in-memory copy is removed, which no
        other answer in this process can also do.
        """
        claim_id = uuid4().hex
        if not pending_store.enabled():
            return claim_id if self.store.take_pending_confirmation(context_id, pending) is not None else None
        claiming = asyncio.ensure_future(pending_store.claim(context_id, pending, claim_id))
        try:
            claimed = await asyncio.shield(claiming)
        except BaseException as exc:
            self._run_to_end(self._release_claim(claiming, context_id, claim_id))
            if not isinstance(exc, Exception):
                raise
            logger.warning("pending_confirmation_claim_failed", exc_info=True)
            raise PendingStoreUnavailable(context_id) from exc
        self.store.clear_pending_confirmation(context_id)
        return claim_id if claimed else None

    async def _release_claim(self, claiming: asyncio.Future, context_id: str, claim_id: str) -> None:
        try:
            if not await claiming:
                return  # nothing was claimed
        except Exception:
            pass  # the outcome is unknown: the database may have applied it
        try:
            await pending_store.release(context_id, claim_id)
        except Exception:
            logger.warning("pending_confirmation_release_failed", exc_info=True)

    @contextlib.asynccontextmanager
    async def _claimed(self, context_id: str, claim_id: str):
        """Hold the claim ``claim_id`` while the body acts on it, then delete the row.

        A claim reads as in progress for CLAIM_TTL_SECONDS after it was last refreshed, and
        an agent call can run far longer (registered agents up to an hour), so the claim is
        refreshed every CLAIM_REFRESH_SECONDS until the body ends."""
        done = asyncio.Event()
        refreshing = asyncio.ensure_future(self._refresh_claim(context_id, claim_id, done))
        try:
            yield
        finally:
            done.set()
            refreshing.cancel()
            await self._finish_pending(context_id, claim_id)

    async def _refresh_claim(self, context_id: str, claim_id: str, done: asyncio.Event) -> None:
        if not pending_store.enabled():
            return  # no claim to keep: the in-memory copy was removed
        # ``done`` ends the loop even when a cancelled refresh comes back as a database
        # error rather than a cancellation (asyncpg can do that).
        while not done.is_set():
            await asyncio.sleep(pending_store.CLAIM_REFRESH_SECONDS)
            try:
                # Bounded (pending_store.refresh also bounds giving its connection back),
                # so one hung connection can't stop the refreshing.
                await asyncio.wait_for(
                    pending_store.refresh(context_id, claim_id), pending_store.CLAIM_REFRESH_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.warning("pending_confirmation_refresh_failed", exc_info=True)

    async def _finish_pending(self, context_id: str, claim_id: str) -> None:
        """Delete the claimed row once its answer is done, unless a newer prompt replaced it.

        The delete runs to the end even if the answer is cancelled: closing the chat stream
        cancels the answer mid-call, and a claim left behind would turn away every answer to
        the prompt until it expired. If the delete fails, the claim expires after
        CLAIM_TTL_SECONDS and is never claimed again.
        """
        if not pending_store.enabled():
            return  # the claim already removed the in-memory copy

        async def delete() -> None:
            try:
                await pending_store.finish(context_id, claim_id)
            except Exception:
                logger.warning("pending_confirmation_finish_failed", exc_info=True)

        await asyncio.shield(self._run_to_end(delete()))

    def _run_to_end(self, cleanup: Awaitable[None]) -> asyncio.Future:
        """Start a claim cleanup that the answer's cancellation must not stop, and hold it
        until it ends."""
        task = asyncio.ensure_future(cleanup)
        self._cleanups.add(task)
        task.add_done_callback(self._cleanups.discard)
        return task

    async def _clear_pending(self, context_id: str) -> None:
        self.store.clear_pending_confirmation(context_id)
        try:
            await pending_store.clear(context_id)
        except Exception:
            logger.warning("pending_confirmation_clear_failed", exc_info=True)

    async def _rehydrate_context(self, context_id: str, channel: Channel) -> ConversationContext | None:
        """Rebuild a working context from persisted history, bound to its true owner.

        Returns None if nothing was persisted for this id. The context is reconstructed
        with its stored owner_email so the caller's ownership check in _open_context is
        honest — a non-owner presenting the id is rejected, not silently served.
        """
        owner = await conversation_store.owner(context_id)
        if owner is None:
            return None
        history = await conversation_store.load_history(context_id) or []
        goal = next((h[6:] for h in reversed(history) if h.startswith("user: ")), "")
        context = ConversationContext(
            context_id=context_id,
            user_id=owner,
            channel=channel,
            current_goal=goal,
            conversation_history=history,
        )
        return self.store.save_context(context)

    def _new_context(
        self,
        user_id: str,
        channel: Channel,
        text: str,
        context_id: str | None = None,
    ) -> ConversationContext:
        return ConversationContext(
            context_id=context_id or str(uuid4()),
            user_id=user_id,
            channel=channel,
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
        context: ConversationContext,
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
                "planner_a2a_failed agent_id=%s skill=%s endpoint=%s",
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
        logger.exception("planner_progress_callback_failed")


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
    # NOTE: an agent's required_credentials are deliberately NOT surfaced to the
    # LLM. Credential handling is out-of-band (Settings + the consent gate in
    # _enforce_credentials); exposing the requirement here made the model solicit
    # API keys in chat. The gate blocks/prompts on call, so the LLM just calls.


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
        path.relative_to(upload_dir().resolve())
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
