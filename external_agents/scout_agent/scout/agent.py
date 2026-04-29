import json
import sys
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from scout.budget import Budget
from scout.capabilities.base import CapabilityRegistry
from scout.config import Settings
from scout.prompts import BUDGET_REACHED_NUDGE, SYSTEM_PROMPT


def _tool_error_msg(tool_call_id: str, error: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": error}


def _serialize_assistant_msg(msg: Any) -> dict:
    out: dict = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return out


async def _react_loop(
    messages: list[dict],
    registry: CapabilityRegistry,
    budget: Budget,
    settings: Settings,
    client: AsyncOpenAI,
    verbose: bool,
) -> str:
    tools = [cap.tool_schema() for cap in registry.all()]

    while True:
        budget_just_reached = budget.iterations_exhausted()
        if budget_just_reached:
            messages.append({"role": "user", "content": BUDGET_REACHED_NUDGE})
        budget.tick_iteration()

        if verbose:
            print(
                f"[scout] iter={budget.iterations_used}/{budget.max_iterations} "
                f"tool_calls={budget.tool_calls_used}/{budget.max_tool_calls}",
                file=sys.stderr,
            )

        kwargs: dict = {
            "model": settings.model,
            "messages": messages,
            "max_completion_tokens": settings.response_max_tokens,
        }
        if tools and not budget_just_reached:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = await client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        messages.append(_serialize_assistant_msg(msg))

        if verbose and resp.usage is not None:
            print(
                f"[scout]   usage: prompt={resp.usage.prompt_tokens} "
                f"completion={resp.usage.completion_tokens} "
                f"finish={choice.finish_reason}",
                file=sys.stderr,
            )

        if not msg.tool_calls:
            return msg.content or ""

        for call in msg.tool_calls:
            tool_name = call.function.name
            if budget.tool_calls_exhausted():
                messages.append(
                    _tool_error_msg(call.id, "tool budget exhausted; do not call tools further")
                )
                if verbose:
                    print(f"[scout]   skipped (budget): {tool_name}", file=sys.stderr)
                continue
            budget.tick_tool_call()

            cap = registry.get(tool_name)
            if cap is None:
                messages.append(_tool_error_msg(call.id, f"unknown tool: {tool_name}"))
                if verbose:
                    print(f"[scout]   unknown tool: {tool_name}", file=sys.stderr)
                continue

            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as e:
                messages.append(_tool_error_msg(call.id, f"invalid tool arguments json: {e}"))
                continue

            if verbose:
                preview = json.dumps(args, ensure_ascii=False)[:120]
                print(f"[scout]   -> {tool_name}({preview})", file=sys.stderr)

            try:
                result = await cap.invoke(args)
            except Exception as e:  # adapter bug; surface to model
                messages.append(_tool_error_msg(call.id, f"tool raised: {e}"))
                continue

            content = (
                json.dumps(result.content, ensure_ascii=False)
                if result.ok
                else f"ERROR: {result.error}"
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

            if verbose:
                status = "ok" if result.ok else f"err({result.error})"
                size = len(content)
                print(f"[scout]   <- {tool_name} [{status}, {size}b]", file=sys.stderr)


@dataclass
class ScoutSession:
    """Multi-turn conversation. Each turn() runs a fresh-budget ReAct loop over the
    accumulated message history. Phase 2 will replace `registry` content with peer
    capabilities; nothing in this class is Brave-specific."""

    registry: CapabilityRegistry
    settings: Settings
    client: AsyncOpenAI
    verbose: bool = False
    messages: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": SYSTEM_PROMPT})

    async def turn(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        budget = Budget(
            max_iterations=self.settings.max_iterations,
            max_tool_calls=self.settings.max_tool_calls,
        )
        return await _react_loop(
            self.messages, self.registry, budget, self.settings, self.client, self.verbose
        )


async def run_mission(
    mission_text: str,
    registry: CapabilityRegistry,
    settings: Settings,
    *,
    client: AsyncOpenAI,
    verbose: bool = False,
) -> str:
    """One-shot helper: open a session, run one turn, return the brief."""
    session = ScoutSession(registry=registry, settings=settings, client=client, verbose=verbose)
    return await session.turn(mission_text)
