"""Autonomous LLM agent loop for the intelligence agent.

The intelligence agent is itself an LLM that reasons over the user's request
for situational awareness / threat assessment / course-of-action analysis,
and decides which tools to invoke. Three tools are exposed:

- ``discover_agents(skill_query)`` — search the AGNTCY Directory for agents
  advertising a particular skill (e.g. ``multi_spider/summarize_recent_tasks``
  for a mission digest from the spider fleet, ``multi_spider/observe`` for a
  fresh SITREP, ``research/open-source-intelligence`` for an OSINT brief).
- ``query_peer_agent(skill_query, prompt, agent_id=None)`` — discover (or
  reuse a previously-discovered) peer agent and send it an A2A
  ``message/send`` request. Returns the peer's text reply plus any
  structured output from its response artifact.
- ``web_search(query, count=5)`` — Brave Search API. Useful when the LLM
  wants raw OSINT signal (news, doctrine, recent events) rather than going
  through a peer agent.

The loop is async: tool handlers are async, and the LLM call uses
``litellm.acompletion``. Tests inject a stub ``completion_fn`` to drive
deterministic conversations without an LLM provider.

The pause/resume machinery from the coding agent (``PendingUserConfirmation``,
``initial_messages``) is preserved here for symmetry — none of the
intelligence tools currently raise it, but if a future tool (e.g. a
hypothetical ``escalate_to_human``) wants to pause the loop, the executor
can persist state and resume just like the coding agent does.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger("intelligence_agent.agent_loop")

ToolHandler = Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]


SYSTEM_PROMPT = """You are an autonomous Intelligence Agent operating over an A2A multi-agent network.

Your job: produce situational awareness, threat assessment, and a recommended course of action (COA) for a user's intelligence request. You synthesize from peer agents (recon / OSINT) and external open sources; you do not have direct sensor access yourself.

You have three tools:

- discover_agents(skill_query): searches the AGNTCY Directory for agents advertising a skill. Use this BEFORE query_peer_agent if you don't already know which peer to talk to.

  The forward recon fleet is the **Multi-Spider Command Center Planner** (custom skill tags namespaced ``multi_spider/*``). Useful skill_query values for it:
    * ``multi_spider/summarize_recent_tasks`` — neutral-prose digest of the last 3 successful spider missions (use this for "what has happened recently?" / mission recap).
    * ``multi_spider/observe`` — fresh tactical SITREP from a multi-frame observation (use for "what is happening right now in zone X?").
    * ``multi_spider/find_target`` — confirmed/not-found check for a named target (tank, person in red, etc.).
    * ``multi_spider/watch_alarm`` — duration-bounded monitor that alarms on detection.
    * ``multi_spider/robot_status`` — fleet availability (which robots are online/busy).
    * ``multi_spider/estimate_distance`` — VLM range estimate to a named target.
    * ``multi_spider/reformat_last_summary`` — restyle the previous summary (SITREP <-> civilian prose) without re-running.

  Other useful peers, when discoverable:
    * ``research/open-source-intelligence`` — OSINT brief from a Scout-style agent.
    * ``intelligence/situation_summary`` — another intel agent (sibling).

  When the user wants a **mission/status summary**, ``multi_spider/summarize_recent_tasks`` is the right entry point; for **current ground truth in a specific area**, use ``multi_spider/observe``; for **target hunting**, use ``multi_spider/find_target``.

- query_peer_agent(skill_query, prompt, agent_id=None): asks a peer agent a natural-language question and returns its reply (plus any structured_output it returned). Pass agent_id when you've already discovered a specific peer; otherwise the tool re-runs discovery and uses the first match for skill_query. Frame the prompt as a clear single question — peer agents are themselves LLM-driven and benefit from focused asks. For the spider fleet, mention the specific skill tag in your prompt too (e.g. "Per multi_spider/summarize_recent_tasks, give me a digest of the last successful missions") so the peer routes correctly.

- web_search(query, count=5): runs a Brave web search and returns the top results (title, url, snippet). Use this for: external context the recon agent does not own (news, doctrine, weather, terrain, prior incidents at this location), corroboration of peer-reported entities, or background on adversary platforms / civilian-population dynamics.

- send_group_email_via_directory(subject, body, importance="normal"|"high"): discover an agent advertising the ``email/group_send`` skill (today: the Gmail-backed email agent) and broadcast ONE message to its locally-managed group recipient list. The email agent owns the recipient list (``notice_list.txt``) — you only provide subject, body, and an optional importance flag. The body should be the full intelligence brief in concise, readable form (not raw JSON). Use this **only** when:
    * the brief describes a HIGH-IMPACT situation that decision-makers beyond the requester need to see — emerging hostile activity, civilian-risk scenario, urgent / time-critical COA, or escalation; OR
    * the user explicitly asked you to broadcast / disseminate / notify the team / email the brief to the group.
  Otherwise, do NOT call this tool — your final reply IS the brief and the requester (PI) will route it. Call AT MOST ONCE per request, AFTER the brief is fully synthesized. Set ``importance="high"`` only when the situation is genuinely time-critical. The recipient list is fixed on the email agent side; you cannot override it from here.

Workflow:
1. Briefly state your intelligence question and what you need to answer it.
2. Pick the right spider-fleet skill for the user's question:
   - "what happened?" / recap / brief → ``multi_spider/summarize_recent_tasks``.
   - "what's there now?" / current sitrep / describe area → ``multi_spider/observe``.
   - "is there a tank/person/X?" → ``multi_spider/find_target``.
   - "alert me if X appears" → ``multi_spider/watch_alarm``.
   - "which robots can we use?" → ``multi_spider/robot_status``.
   Then call query_peer_agent with that skill_query.
3. If useful, follow up with web_search or another query_peer_agent against an OSINT-style skill for context the recon agent doesn't have (terrain, weather, doctrine, similar past engagements).
4. Synthesize a final intelligence brief covering, in this order: SITUATIONAL AWARENESS (what is happening now, who/what/where), THREAT ASSESSMENT (categorized: hostile / civilian / unknown; confidence; immediate vs. emerging), COURSE OF ACTION (2-3 prioritized COAs with tradeoffs and a recommended one), INFORMATION GAPS (what you still don't know and how to fill it).
5. If — and only if — the brief is HIGH-IMPACT (urgent threat, civilian-risk, escalation, or a recommended COA that requires immediate action) OR the user explicitly asked you to disseminate it, call ``send_group_email_via_directory`` ONCE with a clear subject and the brief as the body (``importance="high"`` only for time-critical). Note in your final reply that the brief was broadcast to the group. The brief is still in your final reply regardless.
6. Stop calling tools after step 5 — your final reply IS the brief.

Constraints:
- You are an analyst, not an operator. Recommend actions; do not direct kinetic strikes. The spider fleet has a ``multi_spider/move_robots`` skill — DO NOT call it from this agent.
- Civilians are non-combatants. Always categorize confirmed-civilian observations distinctly from unknown/hostile and flag any COA that risks civilian harm.
- Cite peer agents and web sources by their identifier (peer agent_id or URL) when you reference their findings. Distinguish what you observed from what you inferred.
- Keep each section short — the consumer is a decision-maker who wants signal, not prose. Bulleted sub-headings are fine.
- If the recon fleet is unreachable or returns no data, say so explicitly under INFORMATION GAPS instead of fabricating mission state.
- Default to NOT broadcasting. Routine sitreps, mission recaps, and status checks are not "high-impact" — only escalate to group email when withholding the brief would materially delay a needed decision.
"""


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "discover_agents",
            "description": (
                "Search the AGNTCY Directory for agents claiming a skill. "
                "Returns a list of agents with endpoint, name, description, "
                "and the skills each advertises. Use this before "
                "query_peer_agent when you don't yet know the peer's id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_query": {
                        "type": "string",
                        "description": (
                            "Skill tag to search for. The spider recon fleet "
                            "advertises 'multi_spider/summarize_recent_tasks', "
                            "'multi_spider/observe', 'multi_spider/find_target', "
                            "'multi_spider/watch_alarm', 'multi_spider/robot_status', "
                            "'multi_spider/estimate_distance', "
                            "'multi_spider/reformat_last_summary'. Other peers "
                            "advertise 'research/open-source-intelligence' "
                            "(OSINT) or 'intelligence/situation_summary' "
                            "(sibling intel agent)."
                        ),
                    }
                },
                "required": ["skill_query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_peer_agent",
            "description": (
                "Send a natural-language A2A message/send request to a peer "
                "agent and return its reply text plus any structured_output "
                "from its response artifact. If agent_id is omitted, the "
                "tool re-runs discover_agents(skill_query) and uses the "
                "first match."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_query": {
                        "type": "string",
                        "description": (
                            "Skill tag identifying the kind of peer agent to "
                            "ask. For the spider recon fleet pick the "
                            "specific multi_spider/* skill that fits: "
                            "summarize_recent_tasks (mission digest), observe "
                            "(fresh SITREP), find_target (named target), "
                            "watch_alarm (monitor with alarm), robot_status "
                            "(fleet availability), estimate_distance, "
                            "reformat_last_summary."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The natural-language question or task to send "
                            "to the peer. Phrase as a focused single question."
                        ),
                    },
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "Optional. Specific peer agent_id from a prior "
                            "discover_agents call. When set, skill_query is "
                            "still used to populate the request metadata."
                        ),
                    },
                },
                "required": ["skill_query", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Run a Brave web search and return the top results "
                "(title, url, snippet). Use for OSINT context the peer "
                "recon/scout agents do not own."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {
                        "type": "integer",
                        "description": "Max results to return (1-20). Default 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_group_email_via_directory",
            "description": (
                "Discover an agent advertising the 'email/group_send' skill "
                "(today: the Gmail-backed email agent) and broadcast ONE "
                "message to its locally-managed group recipient list "
                "(notice_list.txt on the email agent). You DO NOT supply "
                "recipients — only subject + body + optional importance. "
                "Call AT MOST ONCE per request and only when the brief is "
                "high-impact (urgent threat, civilian risk, escalation, "
                "time-critical COA) or the user explicitly asked you to "
                "broadcast / disseminate it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": (
                            "Concise email subject (e.g. 'INTEL BRIEF: hostile "
                            "activity in ALPHA-3' or 'SITREP — area BRAVO-2'). "
                            "Keep under ~80 chars."
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "The full intelligence brief in readable plaintext "
                            "(not JSON). Include the same SITUATIONAL "
                            "AWARENESS / THREAT ASSESSMENT / COURSE OF ACTION "
                            "/ INFORMATION GAPS sections you put in your final "
                            "reply, condensed for an email reader."
                        ),
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["normal", "high"],
                        "description": (
                            "Use 'high' only when the situation is genuinely "
                            "time-critical (active hostile activity, civilian "
                            "risk, urgent COA). Default 'normal'."
                        ),
                    },
                },
                "required": ["subject", "body"],
            },
        },
    },
]


class PendingUserConfirmation(Exception):
    """Raised by a tool handler to pause the agent loop pending user input.

    Symmetric to the coding agent's pause mechanism. Currently no
    intelligence tool raises this, but the machinery is kept so the
    executor can adopt it later (e.g. for an ``escalate_to_human`` tool).
    """

    def __init__(self, prompt: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(prompt)
        self.prompt = prompt
        self.payload = payload or {}


@dataclass
class ToolInvocation:
    name: str
    args: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class AgentTrace:
    final_message: str = ""
    exit_reason: str = "completed"  # completed | input_required | step_limit | time_limit | error
    n_steps: int = 0
    elapsed: float = 0.0
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[ToolInvocation] = field(default_factory=list)
    pending: dict[str, Any] | None = None


def _coerce_args(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"_raw": arguments}
        if isinstance(parsed, Mapping):
            return dict(parsed)
        return {"_value": parsed}
    return {}


def _serialize_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    """Normalize litellm/openai tool_call objects into JSON-able dicts."""
    out: list[dict[str, Any]] = []
    for tc in tool_calls or []:
        if isinstance(tc, Mapping):
            fn = tc.get("function") or {}
            out.append({
                "id": tc.get("id"),
                "type": tc.get("type", "function"),
                "function": {
                    "name": fn.get("name") if isinstance(fn, Mapping) else getattr(fn, "name", None),
                    "arguments": fn.get("arguments") if isinstance(fn, Mapping) else getattr(fn, "arguments", ""),
                },
            })
        else:
            fn = getattr(tc, "function", None)
            out.append({
                "id": getattr(tc, "id", None),
                "type": getattr(tc, "type", "function"),
                "function": {
                    "name": getattr(fn, "name", None) if fn else None,
                    "arguments": getattr(fn, "arguments", "") if fn else "",
                },
            })
    return out


async def run_agent_loop(
    user_prompt: str,
    *,
    model: str,
    tool_handlers: dict[str, ToolHandler],
    completion_fn: Callable[..., Awaitable[Any]] | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    step_limit: int = 8,
    time_limit_s: float = 600.0,
    initial_messages: list[dict[str, Any]] | None = None,
    initial_tool_invocations: list[ToolInvocation] | None = None,
    initial_n_steps: int = 0,
) -> AgentTrace:
    """Drive the LLM agent loop until it stops calling tools or hits a limit.

    Mirrors the coding agent's loop so the executor pattern (pause/resume,
    step/time budgets, structured trace) stays consistent across external
    agents in the network.
    """
    if completion_fn is None:
        import litellm  # imported lazily so tests don't need it
        completion_fn = litellm.acompletion

    if initial_messages is not None:
        messages: list[dict[str, Any]] = list(initial_messages)
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    trace = AgentTrace(messages=messages)
    if initial_tool_invocations:
        trace.tool_calls.extend(initial_tool_invocations)
    trace.n_steps = initial_n_steps
    started = time.monotonic()

    while True:
        if trace.n_steps >= step_limit:
            trace.exit_reason = "step_limit"
            trace.final_message = trace.final_message or "(step limit reached)"
            break
        if (time.monotonic() - started) >= time_limit_s:
            trace.exit_reason = "time_limit"
            trace.final_message = trace.final_message or "(time limit reached)"
            break

        trace.n_steps += 1
        logger.info(
            "agent_loop step=%d messages=%d tools_used=%d",
            trace.n_steps, len(messages), len(trace.tool_calls),
        )
        response = await completion_fn(
            model=model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
        )

        choice = response.choices[0]
        msg = choice.message
        content = getattr(msg, "content", None)
        if isinstance(msg, Mapping):
            content = msg.get("content", content)
            raw_tool_calls = msg.get("tool_calls")
        else:
            raw_tool_calls = getattr(msg, "tool_calls", None)
        tool_calls = _serialize_tool_calls(raw_tool_calls)

        assistant_message: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)

        if not tool_calls:
            trace.final_message = (content or "").strip()
            trace.exit_reason = "completed"
            break

        paused = False
        for call_index, call in enumerate(tool_calls):
            tool_name = (call.get("function") or {}).get("name") or ""
            args = _coerce_args((call.get("function") or {}).get("arguments"))
            invocation = ToolInvocation(name=tool_name, args=args)
            trace.tool_calls.append(invocation)
            logger.info(
                "agent_loop tool_call name=%s args_keys=%s",
                tool_name, sorted(args.keys()),
            )

            handler = tool_handlers.get(tool_name)
            if handler is None:
                invocation.error = f"unknown tool {tool_name!r}"
                tool_result: dict[str, Any] = {"error": invocation.error}
            else:
                try:
                    raw_result = await handler(args)
                    if asyncio.iscoroutine(raw_result):
                        raw_result = await raw_result
                    tool_result = (
                        raw_result if isinstance(raw_result, dict) else {"value": raw_result}
                    )
                    invocation.result = tool_result
                except PendingUserConfirmation as pause:
                    invocation.result = {
                        "status": "pending_user_confirmation",
                        "prompt": pause.prompt,
                    }
                    trace.pending = {
                        "tool_call_id": call.get("id"),
                        "tool_name": tool_name,
                        "args": args,
                        "prompt": pause.prompt,
                        "payload": pause.payload,
                    }
                    trace.exit_reason = "input_required"
                    trace.final_message = pause.prompt
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": tool_name,
                        "content": json.dumps(invocation.result, default=str),
                    })
                    for skipped in tool_calls[call_index + 1 :]:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": skipped.get("id"),
                            "name": (skipped.get("function") or {}).get("name") or "",
                            "content": json.dumps({
                                "skipped": "deferred until user confirmation"
                            }),
                        })
                    paused = True
                    logger.info(
                        "agent_loop paused: tool=%s prompt=%r",
                        tool_name, pause.prompt[:120],
                    )
                    break
                except Exception as exc:
                    invocation.error = f"{type(exc).__name__}: {exc}"
                    tool_result = {"error": invocation.error}
                    logger.exception("tool %s raised", tool_name)

            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "name": tool_name,
                "content": json.dumps(tool_result, default=str),
            })

        if paused:
            break

    trace.elapsed = time.monotonic() - started
    logger.info(
        "agent_loop done exit=%s steps=%d tools=%d elapsed=%.2fs",
        trace.exit_reason, trace.n_steps, len(trace.tool_calls), trace.elapsed,
    )
    return trace


def serialize_invocations(invocations: list[ToolInvocation]) -> list[dict[str, Any]]:
    return [
        {"name": inv.name, "args": inv.args, "result": inv.result, "error": inv.error}
        for inv in invocations
    ]


def deserialize_invocations(data: list[dict[str, Any]]) -> list[ToolInvocation]:
    return [
        ToolInvocation(
            name=str(d.get("name") or ""),
            args=dict(d.get("args") or {}),
            result=d.get("result") if isinstance(d.get("result"), dict) else None,
            error=d.get("error"),
        )
        for d in (data or [])
    ]
