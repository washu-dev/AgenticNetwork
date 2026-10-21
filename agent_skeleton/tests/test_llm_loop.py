"""run_tool_loop at its step limit: the closing call can't call tools, and the loop
never returns an empty answer. The OpenAI client is faked; no network."""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_skeleton import llm_loop

TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]


def _text(content):
    return SimpleNamespace(role="assistant", content=content, tool_calls=None)


def _tool_call(n):
    call = SimpleNamespace(id=f"call-{n}", function=SimpleNamespace(name="lookup", arguments=json.dumps({"q": "x"})))
    return SimpleNamespace(role="assistant", content=None, tool_calls=[call])


class _Client:
    """Records every chat.completions.create call; ``reply(kwargs, n)`` gives the
    assistant message for the n-th call."""

    def __init__(self, reply) -> None:
        self.calls: list[dict] = []
        self._reply = reply
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(message=self._reply(kwargs, len(self.calls)))])


def _loop(monkeypatch, client, max_steps):
    monkeypatch.setattr(llm_loop, "build_openai_client", lambda: client)
    return llm_loop.run_tool_loop(
        model="m",
        system_prompt="s",
        user_payload={"prompt": "hi"},
        tool_schemas=TOOLS,
        dispatch=lambda name, args: {"ok": True},
        max_steps=max_steps,
    )


def test_run_tool_loop_final_call_after_step_limit_offers_no_tools(monkeypatch):
    # Asks for a tool whenever it may; answers in text once tools are disabled.
    client = _Client(lambda kw, n: _text("summary") if kw.get("tool_choice") == "none" else _tool_call(n))
    text, tool_log = _loop(monkeypatch, client, max_steps=3)

    assert text == "summary"
    assert len(tool_log) == 3
    # The initial call plus one per step; only the last one disables tools.
    assert [c.get("tool_choice") for c in client.calls] == [None, None, None, "none"]


def test_run_tool_loop_returns_step_limit_message_instead_of_empty_text(monkeypatch):
    client = _Client(lambda kw, n: _tool_call(n))  # asks for a tool even when told not to
    text, tool_log = _loop(monkeypatch, client, max_steps=2)

    assert len(tool_log) == 2
    assert "2-step tool limit" in text


def test_run_tool_loop_answer_within_limit_is_unchanged(monkeypatch):
    client = _Client(lambda kw, n: _tool_call(n) if n == 1 else _text("done"))
    text, tool_log = _loop(monkeypatch, client, max_steps=3)

    assert (text, len(tool_log)) == ("done", 1)
    assert all("tool_choice" not in c for c in client.calls)
