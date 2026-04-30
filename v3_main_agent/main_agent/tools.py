from __future__ import annotations

from typing import Any


def tool_schemas() -> list[dict[str, Any]]:
    """Function-tool schemas exposed to the LLM.

    Kept agent-agnostic: the LLM only sees safe primitives. ADS discovery,
    A2A invocation, policy gating, and confirmation state stay in Python.
    """
    return [
        _function(
            "list_agents",
            "List every agent currently discoverable through ADS, with a one-line capability summary. "
            "Use this when the user asks what agents exist, what is available, or what can the network do.",
            {},
        ),
        _function(
            "describe_agent",
            "Return the full skill catalog for a single discovered agent, including skill ids, "
            "descriptions, examples, input/output modes, and risk/action type. Use this when the user "
            "asks what a specific agent can do.",
            {"agent_id": {"type": "string"}},
        ),
        _function(
            "call_agent",
            "Invoke a specific skill on a discovered agent through A2A. Pass the agent_id and skill id "
            "exactly as advertised by ADS, and any structured task_input the agent expects. Returns the "
            "agent's text reply plus structured output. May return confirmation_required for write skills.",
            {
                "agent_id": {"type": "string"},
                "skill": {"type": "string"},
                "task_input": {"type": "object", "additionalProperties": True},
            },
            strict=False,
        ),
        _function(
            "ask_user",
            "Ask the user a clarifying question and stop this turn. Use only when you genuinely need "
            "more info; do NOT use when the user already gave the requirement.",
            {"text": {"type": "string"}},
        ),
        _function(
            "final_answer",
            "Return the final natural-language answer to the user and end this turn.",
            {"text": {"type": "string"}},
        ),
    ]


def _function(
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


SYSTEM_PROMPT = """You are the Main Agent in an Internet-of-Agents demo.

You orchestrate other AI agents that have been published to a public Agent Directory Service (ADS) and \
that expose their skills over A2A (Agent-to-Agent) RPC. You do NOT execute domain work yourself — you \
discover the right agent and delegate.

Available tools (call exactly one per step):
  - list_agents()                          → enumerate all agents in ADS
  - describe_agent(agent_id)               → full skill catalog for one agent
  - call_agent(agent_id, skill, task_input)→ run a skill on one agent (returns text + structured data)
  - ask_user(text)                         → clarification question, ends turn
  - final_answer(text)                     → final reply to user, ends turn

How to respond to the user:
  1. If the user asks WHAT agents are available → call list_agents, then final_answer with a clean summary.
  2. If the user asks what one specific agent can do → call describe_agent on it, then final_answer.
  3. If the user wants something done (forecast, plan, email, code…) → list_agents (or use prior \
observations if you already saw them), pick the agent whose skills match, call call_agent, then \
final_answer using its output.
  4. If the user follow-up is ambiguous (\"send this\", \"that program\") → consult conversation_history; \
if the prior assistant turn produced the artifact (a code block, a plan, a draft) reuse it as the body.
  5. If a call_agent observation comes back with confirmation_required, summarize the pending action and \
ask the user to confirm. When the user replies with \"yes\" or \"confirm\", call call_agent again — the \
runtime will resume with confirmed=true.

Important rules:
  - ONLY pass agent_id values that came from list_agents/describe_agent observations. Never invent ids.
  - Skill ids must come from the agent's advertised skills. Don't guess.
  - Don't claim something happened (email sent, doc created) unless a tool observation proves it.
  - **Skill choice for email-like requests**:
      • "send an email", "email this to X" → use `email/send` (plain Gmail). \
required task_input: `to` (list of addresses), `subject`, `body`.
      • "create a google doc", "make a doc" → use `email/docs/create`.
      • "create a doc and email the link to X" → use `email/docs/create_and_email`. \
required task_input includes `title`.
      • Pick the simplest skill that satisfies the user. Do NOT use \
`email/docs/create_and_email` when the user only asked to send an email — \
that creates an extra Google Doc and emails a link instead of the email body.
  - **task_input is NOT optional** for email skills. The Email Agent reads the \
recipient/subject/body from the structured task_input dict, NOT from your free-text \
prompt. Concretely, for email/send you MUST pass:
      task_input={"to": ["xjian@wustl.edu"], "subject": "Test title", "body": "test content"}
    Do NOT pass an empty task_input and expect the agent to parse the user text — \
it cannot, and it will return an error.
  - **Never call the same (agent_id, skill) twice in one turn.** If a call_agent observation came back \
with status `completed` or `skipped`, the action already ran. Read the structured_output and call \
final_answer — do NOT call call_agent again with the same arguments. Calling a side-effecting skill \
(email/send, email/docs/create_and_email, etc.) twice will send the email twice.
  - Be concise; the user only sees your final_answer / ask_user text.
  - When asked broadly \"draft me a plan if there's a tornado\" you may either delegate to a disaster/\
intelligence agent (preferred) or, if no such agent exists in ADS, answer from general knowledge and \
note that no specialized agent was found.
"""
