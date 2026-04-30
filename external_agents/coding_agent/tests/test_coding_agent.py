"""End-to-end coverage for the mini-swe-agent-backed coding executor.

These tests inject fake runners into ``CodingAgentExecutor`` so they exercise
the full A2A round-trip (RequestContext -> EventQueue -> Task -> A2ATaskResult)
*and* the workspace seed/capture mechanics without needing a live LLM.

They run from the repo root with the AgenticNetwork venv:

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \\
    PYTHONPATH=external_agents/coding_agent \\
    pytest external_agents/coding_agent/tests -q
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest


_THIS = Path(__file__).resolve()
_AGENT_DIR = _THIS.parents[1]
_REPO_ROOT = _AGENT_DIR.parents[1]


def _load_coding_agent_module():
    spec = importlib.util.spec_from_file_location(
        "external_coding_agent", _AGENT_DIR / "coding_agent.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("external_coding_agent", module)
    spec.loader.exec_module(module)
    return module


coding_agent = _load_coding_agent_module()
sys.path.insert(0, str(_REPO_ROOT))

from v2_agent_network.services.a2a.adapters import a2a_agent_card_to_discovered_agent
from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.a2a.transport import HTTPA2ATransport
from v2_agent_network.services.common.models import (
    A2AMessage,
    A2APart,
    A2ATaskRequest,
    A2ATaskState,
)


CODING_CARD = _REPO_ROOT / "v2_agent_network/configs/agent_cards/coding_agent.card.json"


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _make_runner(submission, *, files_to_create: dict[str, str] | None = None,
                 n_calls: int = 4, exit_status: str = "Submitted"):
    payload = submission if isinstance(submission, str) else json.dumps(submission)

    def runner(*, task: str, skill: str, workspace: str, limits: dict) -> dict:
        runner.last_task = task
        runner.last_skill = skill
        runner.last_workspace = workspace
        runner.last_limits = limits
        if files_to_create:
            for rel, content in files_to_create.items():
                target = Path(workspace) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        return {
            "submission": payload,
            "exit_status": exit_status,
            "n_calls": n_calls,
            "cost": 0.0,
            "messages": [{"role": "assistant", "content": payload}],
        }

    runner.last_task = None
    runner.last_skill = None
    runner.last_workspace = None
    runner.last_limits = None
    return runner


import types


def _stub_response(*, content=None, tool_calls=None):
    """Build a SimpleNamespace mimicking litellm.acompletion's response shape."""
    raw_calls = []
    for i, (name, args) in enumerate(tool_calls or []):
        raw_calls.append(
            types.SimpleNamespace(
                id=f"call_{i}",
                type="function",
                function=types.SimpleNamespace(
                    name=name,
                    arguments=json.dumps(args),
                ),
            )
        )
    msg = types.SimpleNamespace(
        content=content,
        tool_calls=raw_calls or None,
    )
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def _completion_stub(plan):
    """Return an async ``acompletion`` stub that yields ``plan`` steps in order.

    Each plan step is either ``("tool", name, args)`` to emit a single tool
    call, or ``("final", text)`` to emit a no-tool-calls assistant message
    that ends the agent loop.
    """
    iterator = iter(plan)

    async def acompletion(**kwargs):
        try:
            step = next(iterator)
        except StopIteration:
            raise AssertionError("agent loop continued past planned steps")
        kind = step[0]
        if kind == "final":
            return _stub_response(content=step[1])
        if kind == "tool":
            return _stub_response(tool_calls=[(step[1], step[2])])
        raise AssertionError(f"unknown plan step {step!r}")

    return acompletion


def _default_plan(prompt: str) -> list:
    """One run_coder call with the prompt as task, then finish."""
    return [
        ("tool", "run_coder", {"task": prompt}),
        ("final", "done"),
    ]


async def _send(skill: str, prompt: str, runner, *, source_files=None, tmp_path=None,
                file_parts=None, plan=None, cleanup=True) -> dict:
    card = coding_agent.load_coding_a2a_agent_card(CODING_CARD)
    executor = coding_agent.CodingAgentExecutor(
        agent_runner=runner,
        workspace_root=str(tmp_path) if tmp_path else None,
        completion_fn=_completion_stub(plan or _default_plan(prompt)),
        cleanup=cleanup,
    )
    app = coding_agent.create_coding_a2a_app(card, executor=executor)
    discovered = a2a_agent_card_to_discovered_agent(card)
    parts = [A2APart(kind="text", text=prompt)]
    if file_parts:
        parts.extend(file_parts)
    metadata = {"skill": skill}
    if source_files is not None:
        metadata["source_files"] = source_files
    request = A2ATaskRequest(
        task_id="t-1",
        context_id="ctx-1",
        target_agent_id=discovered.agent_id,
        skill=skill,
        user_goal=prompt,
        messages=[A2AMessage(role="user", parts=parts)],
        metadata=metadata,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=card.url,
    ) as http_client:
        transport = HTTPA2ATransport(
            http_clients_by_origin={_origin(card.url): http_client}
        )
        client = A2AClient(transport)
        return (await client.send_task(discovered, request)).model_dump(mode="json")


def test_documentation_returns_generated_files_in_response(tmp_path) -> None:
    runner = _make_runner(
        {
            "documentation_draft": "Drafted README.md and docs/usage.md.",
            "target_files": ["README.md", "docs/usage.md"],
            "risk_level": "low",
        },
        files_to_create={
            "README.md": "# Demo\n\nGenerated by the coding agent.\n",
            "docs/usage.md": "Usage instructions.\n",
        },
    )

    result = asyncio.run(
        _send("code/documentation", "Document this module.", runner, tmp_path=tmp_path)
    )

    assert result["state"] == A2ATaskState.completed.value
    structured = result["structured_output"]
    paths = {entry["path"] for entry in structured["generated_files"]}
    assert paths == {"README.md", "docs/usage.md"}
    actions = {entry["action"] for entry in structured["generated_files"]}
    assert actions == {"created"}
    readme = next(e for e in structured["generated_files"] if e["path"] == "README.md")
    assert readme["content"].startswith("# Demo")
    assert readme["content_truncated"] is False
    # The agent loop drove a single run_coder tool call.
    tool_names = [tc["name"] for tc in structured["agent"]["tool_calls"]]
    assert tool_names == ["run_coder"]
    # The artifact carries one DataPart with the structured output; file
    # contents ride inline inside generated_files (no FilePart emission, per
    # the v2 design rule that file payloads should be small text inline or
    # large content via URIs — coding outputs fit in the inline path).
    artifact = result["artifacts"][0]
    assert {p["kind"] for p in artifact["parts"]} == {"data"}


def test_seeded_source_files_are_visible_to_runner_and_modified_files_returned(tmp_path) -> None:
    runner = _make_runner(
        {
            "proposed_action": {
                "summary": "Patched the off-by-one bug in foo.py.",
                "steps": ["Update loop bound."],
                "modifies_repository": True,
            },
            "risk_level": "medium",
            "confirmation_required": True,
        },
        files_to_create={"src/foo.py": "def foo(items):\n    return items[:-1]\n"},
    )

    source_files = [
        {"path": "src/foo.py", "content": "def foo(items):\n    return items[:-2]\n"},
        {"path": "tests/test_foo.py", "content": "from src.foo import foo\n"},
    ]

    result = asyncio.run(
        _send(
            "code/bug_fix",
            "Fix the off-by-one in src/foo.py.",
            runner,
            source_files=source_files,
            tmp_path=tmp_path,
            plan=[
                ("tool", "run_coder",
                 {"task": "Patch the off-by-one bug in src/foo.py and re-check tests/test_foo.py."}),
                ("final", "fixed"),
            ],
        )
    )

    assert result["state"] == A2ATaskState.completed.value
    structured = result["structured_output"]
    seeded_paths = sorted(entry["path"] for entry in structured["seeded_files"])
    assert seeded_paths == ["src/foo.py", "tests/test_foo.py"]
    foo_change = next(
        entry for entry in structured["generated_files"] if entry["path"] == "src/foo.py"
    )
    assert foo_change["action"] == "modified"
    assert "items[:-1]" in foo_change["content"]
    # Untouched seeded files are not returned.
    assert all(entry["path"] != "tests/test_foo.py" for entry in structured["generated_files"])
    # The LLM-supplied task is what the runner saw.
    assert "src/foo.py" in runner.last_task


def test_workspace_is_cleaned_up_by_default(tmp_path) -> None:
    runner = _make_runner(
        {"summary": "ok", "risk_level": "low"},
        files_to_create={"out.txt": "hi"},
    )
    asyncio.run(_send("code/repository_inspection", "Inspect.", runner, tmp_path=tmp_path))
    # tmp_path is the workspace_root parent — the per-task scratch dirs under
    # it should all be gone once the response was emitted. ``_state`` is a
    # housekeeping dir for paused-task continuation state and is expected to
    # remain (empty) after a completed task.
    leftover_dirs = [p for p in tmp_path.iterdir() if p.is_dir() and p.name != "_state"]
    assert leftover_dirs == [], leftover_dirs


def test_keep_workspace_preserves_dir(tmp_path) -> None:
    runner = _make_runner(
        {"summary": "ok", "risk_level": "low"},
        files_to_create={"out.txt": "hi"},
    )
    card = coding_agent.load_coding_a2a_agent_card(CODING_CARD)
    executor = coding_agent.CodingAgentExecutor(
        agent_runner=runner,
        workspace_root=str(tmp_path),
        cleanup=False,
        completion_fn=_completion_stub(_default_plan("Inspect.")),
    )
    app = coding_agent.create_coding_a2a_app(card, executor=executor)
    discovered = a2a_agent_card_to_discovered_agent(card)
    request = A2ATaskRequest(
        task_id="keep-1",
        context_id="ctx-keep",
        target_agent_id=discovered.agent_id,
        skill="code/repository_inspection",
        user_goal="Inspect.",
        messages=[A2AMessage(role="user", parts=[A2APart(kind="text", text="Inspect.")])],
        metadata={"skill": "code/repository_inspection"},
    )

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=card.url,
        ) as http_client:
            transport = HTTPA2ATransport(
                http_clients_by_origin={_origin(card.url): http_client}
            )
            client = A2AClient(transport)
            await client.send_task(discovered, request)

    asyncio.run(_go())
    leftover = [p for p in tmp_path.iterdir() if p.is_dir() and p.name != "_state"]
    assert len(leftover) == 1
    assert (leftover[0] / "out.txt").read_text() == "hi"


def test_path_traversal_in_metadata_is_rejected_with_failed_state(tmp_path) -> None:
    runner = _make_runner({"summary": "ok", "risk_level": "low"})
    result = asyncio.run(
        _send(
            "code/repository_inspection",
            "Inspect.",
            runner,
            source_files=[{"path": "../escape.py", "content": "x"}],
            tmp_path=tmp_path,
        )
    )
    assert result["state"] == A2ATaskState.failed.value
    artifact_data = next(
        part for artifact in result["artifacts"]
        for part in artifact["parts"] if part["kind"] == "data"
    )["data"]
    assert "escapes the workspace" in artifact_data["error"]


def test_runner_failure_surfaces_in_tool_trace_with_no_generated_files(tmp_path) -> None:
    def boom(**_kwargs):
        raise RuntimeError("model auth failed")

    result = asyncio.run(
        _send("code/repository_inspection", "Inspect.", boom, tmp_path=tmp_path,
              plan=[
                  ("tool", "run_coder", {"task": "Inspect."}),
                  ("final", "I tried to run the coder but it failed."),
              ])
    )
    # The agent loop catches tool errors and lets the LLM keep going, so the
    # task itself completes — the runner failure shows up in the trace.
    assert result["state"] == A2ATaskState.completed.value
    structured = result["structured_output"]
    assert structured["generated_files"] == []
    run_calls = structured["agent"]["run_coder_calls"]
    assert len(run_calls) == 1
    assert "model auth failed" in run_calls[0]["error"]


def test_large_file_is_returned_truncated(tmp_path) -> None:
    big = "x" * (coding_agent._INLINE_FILE_BYTE_LIMIT + 100)
    runner = _make_runner(
        {"summary": "Generated a big file.", "risk_level": "low"},
        files_to_create={"big.txt": big},
    )
    result = asyncio.run(
        _send("code/repository_inspection", "Inspect.", runner, tmp_path=tmp_path)
    )
    structured = result["structured_output"]
    big_entry = next(e for e in structured["generated_files"] if e["path"] == "big.txt")
    assert big_entry["content_truncated"] is True
    assert "content" not in big_entry
    assert big_entry["size_bytes"] == len(big)


@pytest.mark.parametrize(
    ("submission", "expected_keys"),
    [
        ('```json\n{"a": 1}\n```', {"a"}),
        ('prefix\n{"b": 2}\nsuffix', {"b"}),
        ('{"c": [1, 2, 3]}', {"c"}),
        ("not json at all", set()),
    ],
)
def test_parse_submission_json_handles_common_shapes(submission, expected_keys) -> None:
    parsed = coding_agent._parse_submission_json(submission)
    assert set(parsed) == expected_keys


def test_task_prompt_describes_workspace_and_seeded_files(tmp_path) -> None:
    seeded = [{"path": "src/foo.py", "size_bytes": 42}]
    prompt = coding_agent._task_for_skill(
        "code/bug_fix", "Fix it.", seeded, tmp_path
    )
    assert str(tmp_path) in prompt
    assert "src/foo.py" in prompt
    assert "OFF LIMITS" in prompt
    assert "shipped back to the caller" in prompt
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in prompt


# ---------------------------------------------------------------------------
# Delivery: discover an email agent through the directory and ship the files
# ---------------------------------------------------------------------------


from a2a.server.agent_execution import AgentExecutor
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import DataPart, Part, TextPart

from v2_agent_network.services.directory.client import DirectoryClient


EMAIL_CARD = _REPO_ROOT / "v2_agent_network/configs/agent_cards/email_agent.card.json"


class _RecordingEmailExecutor(AgentExecutor):
    """In-process email agent stub that records what the coding agent sent."""

    def __init__(self) -> None:
        self.received: list[dict] = []

    async def execute(self, context, event_queue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        skill = (context.metadata or {}).get("skill", "")
        if skill != "email/send":
            await updater.failed(
                message=updater.new_agent_message(
                    [Part(root=TextPart(text=f"unsupported skill {skill}"))]
                )
            )
            return
        meta = context.metadata or {}
        if not (meta.get("confirmed") or meta.get("confirmation_status") == "confirmed"):
            await updater.requires_input(
                message=updater.new_agent_message(
                    [Part(root=TextPart(text="email/send requires confirmation"))]
                ),
                final=True,
            )
            return
        payload: dict = {}
        if context.message:
            for part in context.message.parts:
                if isinstance(part.root, DataPart):
                    payload.update(part.root.data)
                elif isinstance(part.root, TextPart) and "body_text_fallback" not in payload:
                    payload["body_text_fallback"] = part.root.text
        self.received.append(payload)
        result = {
            "skill": "email/send",
            "status": "completed",
            "message_id": "msg-test-1",
            "to": payload.get("to"),
            "subject": payload.get("subject"),
        }
        await updater.start_work()
        await updater.add_artifact(
            [Part(root=DataPart(data=result))],
            artifact_id="email-send",
            name="email_send",
            metadata={"skill": "email/send", "content_type": "application/json"},
        )
        await updater.complete(
            message=updater.new_agent_message(
                [Part(root=TextPart(text=f"Sent email to {', '.join(payload.get('to', []))}."))],
                metadata={"structured_output": result},
            )
        )

    async def cancel(self, context, event_queue) -> None:
        TaskUpdater(event_queue, context.task_id, context.context_id)


def _make_email_app(executor: _RecordingEmailExecutor):
    card = coding_agent.load_coding_a2a_agent_card(EMAIL_CARD)
    return card, A2AStarletteApplication(
        agent_card=card,
        http_handler=DefaultRequestHandler(
            agent_executor=executor, task_store=InMemoryTaskStore()
        ),
    ).build()


async def _send_with_delivery(
    *,
    runner,
    plan,
    tmp_path,
    publish_email_card: bool = True,
) -> tuple[dict, _RecordingEmailExecutor]:
    coding_card = coding_agent.load_coding_a2a_agent_card(CODING_CARD)
    email_executor = _RecordingEmailExecutor()
    email_card, email_app = _make_email_app(email_executor)

    directory = DirectoryClient(fake_ads=True)
    if publish_email_card:
        directory.publish_agent_card(email_card)
    directory.publish_agent_card(coding_card)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=email_app), base_url=email_card.url,
    ) as email_http_client:
        delivery_transport = HTTPA2ATransport(
            http_clients_by_origin={_origin(email_card.url): email_http_client}
        )
        executor = coding_agent.CodingAgentExecutor(
            agent_runner=runner,
            workspace_root=str(tmp_path),
            delivery_directory=directory,
            delivery_transport=delivery_transport,
            completion_fn=_completion_stub(plan),
        )
        coding_app = coding_agent.create_coding_a2a_app(coding_card, executor=executor)
        discovered = a2a_agent_card_to_discovered_agent(coding_card)
        request = A2ATaskRequest(
            task_id="t-deliver-1",
            context_id="ctx-deliver",
            target_agent_id=discovered.agent_id,
            skill="code/documentation",
            user_goal="Document this module and email it.",
            messages=[
                A2AMessage(
                    role="user",
                    parts=[A2APart(kind="text", text="Document this module and email it.")],
                )
            ],
            metadata={},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=coding_app), base_url=coding_card.url,
        ) as coding_http_client:
            transport = HTTPA2ATransport(
                http_clients_by_origin={_origin(coding_card.url): coding_http_client}
            )
            client = A2AClient(transport)
            result = await client.send_task(discovered, request)
    return result.model_dump(mode="json"), email_executor


README_CONTENT = "# Hello\n\nThe coding agent wrote me.\n"


def test_delivery_routes_generated_files_to_discovered_email_agent(tmp_path) -> None:
    runner = _make_runner(
        {"documentation_draft": "Drafted README.", "risk_level": "low"},
        files_to_create={"README.md": README_CONTENT},
    )
    plan = [
        ("tool", "run_coder", {"task": "Write README.md describing this module."}),
        ("tool", "send_email_via_directory", {
            "to": ["alice@example.com"],
            "subject": "Generated docs",
            "body": "Here's the README we asked for.",
            "files": [{"path": "README.md", "content": README_CONTENT}],
            "confirmed": True,
        }),
        ("final", "Sent the README to Alice."),
    ]

    result, email_mock = asyncio.run(
        _send_with_delivery(runner=runner, plan=plan, tmp_path=tmp_path)
    )

    assert result["state"] == A2ATaskState.completed.value
    delivery_results = result["structured_output"]["delivery_results"]
    assert len(delivery_results) == 1
    delivery_result = delivery_results[0]
    assert delivery_result["status"] == "sent"
    assert delivery_result["agent_id"] == "email-agent"
    assert delivery_result["to"] == ["alice@example.com"]
    assert delivery_result["subject"] == "Generated docs"
    assert delivery_result["file_count"] == 1
    assert delivery_result["send_state"] == A2ATaskState.completed.value
    assert delivery_result["send_message_id"] == "msg-test-1"

    # The email mock saw the formatted body containing the file content.
    assert len(email_mock.received) == 1
    sent = email_mock.received[0]
    assert sent["to"] == ["alice@example.com"]
    assert sent["subject"] == "Generated docs"
    assert "README.md" in sent["body"]
    assert "The coding agent wrote me." in sent["body"]
    assert "Here's the README we asked for." in sent["body"]


def test_delivery_skipped_when_agent_does_not_call_send_email(tmp_path) -> None:
    runner = _make_runner(
        {"documentation_draft": "ok", "risk_level": "low"},
        files_to_create={"README.md": "# Hello\n"},
    )
    plan = [
        ("tool", "run_coder", {"task": "Document this module."}),
        ("final", "Drafted README.md but no delivery requested."),
    ]
    result, email_mock = asyncio.run(
        _send_with_delivery(runner=runner, plan=plan, tmp_path=tmp_path)
    )
    assert result["state"] == A2ATaskState.completed.value
    assert result["structured_output"]["delivery_results"] == []
    assert email_mock.received == []


def test_delivery_no_files_status_when_runner_creates_nothing(tmp_path) -> None:
    runner = _make_runner({"documentation_draft": "no files", "risk_level": "low"})
    plan = [
        ("tool", "run_coder", {"task": "Inspect."}),
        ("tool", "send_email_via_directory", {
            "to": ["alice@example.com"],
            "subject": "(empty)",
            "body": "Nothing to send.",
            "files": [],
        }),
        ("final", "tried"),
    ]
    result, email_mock = asyncio.run(
        _send_with_delivery(runner=runner, plan=plan, tmp_path=tmp_path)
    )
    delivery_results = result["structured_output"]["delivery_results"]
    assert delivery_results[0]["status"] == "no_files"
    assert email_mock.received == []


def test_delivery_missing_recipient_is_recorded(tmp_path) -> None:
    runner = _make_runner(
        {"documentation_draft": "ok", "risk_level": "low"},
        files_to_create={"README.md": "# Hello\n"},
    )
    plan = [
        ("tool", "run_coder", {"task": "Document."}),
        ("tool", "send_email_via_directory", {
            "to": [],
            "subject": "(no recipient)",
            "body": "Where to?",
            "files": [{"path": "README.md", "content": "# Hello\n"}],
        }),
        ("final", "tried"),
    ]
    result, email_mock = asyncio.run(
        _send_with_delivery(runner=runner, plan=plan, tmp_path=tmp_path)
    )
    delivery_results = result["structured_output"]["delivery_results"]
    assert delivery_results[0]["status"] == "missing_recipient"
    assert email_mock.received == []


def test_delivery_no_agent_found_when_directory_is_empty(tmp_path) -> None:
    """Same setup but the email card is never published — discovery misses."""
    runner = _make_runner(
        {"documentation_draft": "ok", "risk_level": "low"},
        files_to_create={"README.md": "# Hello\n"},
    )
    plan = [
        ("tool", "run_coder", {"task": "Document."}),
        ("tool", "send_email_via_directory", {
            "to": ["alice@example.com"],
            "subject": "Generated docs",
            "body": "Here's the README.",
            "files": [{"path": "README.md", "content": "# Hello\n"}],
        }),
        ("final", "tried"),
    ]
    result, _email_mock = asyncio.run(
        _send_with_delivery(
            runner=runner, plan=plan, tmp_path=tmp_path, publish_email_card=False,
        )
    )
    delivery_results = result["structured_output"]["delivery_results"]
    assert delivery_results[0]["status"] == "no_agent_found"
    assert delivery_results[0]["skill_query"] == "email/send"


# ---------------------------------------------------------------------------
# Pause + resume: email confirmation routes through A2A input-required
# ---------------------------------------------------------------------------


async def _send_with_resume(
    *,
    runner,
    first_plan,
    second_prompt: str,
    second_plan,
    tmp_path,
) -> tuple[dict, dict, _RecordingEmailExecutor, list]:
    """Drive two A2A requests against the same coding executor + task_id.

    The first request paints into the executor's state store; the second
    targets the same ``task_id`` to exercise the resume path.
    """
    coding_card = coding_agent.load_coding_a2a_agent_card(CODING_CARD)
    email_executor = _RecordingEmailExecutor()
    email_card, email_app = _make_email_app(email_executor)

    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(email_card)
    directory.publish_agent_card(coding_card)

    completion_calls: list[dict] = []

    plan_iter = iter(first_plan + second_plan)

    async def acompletion(**kwargs):
        completion_calls.append({
            "messages": list(kwargs.get("messages") or []),
            "model": kwargs.get("model"),
        })
        try:
            step = next(plan_iter)
        except StopIteration:
            raise AssertionError("agent loop continued past planned steps")
        if step[0] == "final":
            return _stub_response(content=step[1])
        if step[0] == "tool":
            return _stub_response(tool_calls=[(step[1], step[2])])
        raise AssertionError(f"unknown plan step {step!r}")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=email_app), base_url=email_card.url,
    ) as email_http_client:
        delivery_transport = HTTPA2ATransport(
            http_clients_by_origin={_origin(email_card.url): email_http_client}
        )
        executor = coding_agent.CodingAgentExecutor(
            agent_runner=runner,
            workspace_root=str(tmp_path),
            delivery_directory=directory,
            delivery_transport=delivery_transport,
            completion_fn=acompletion,
        )
        coding_app = coding_agent.create_coding_a2a_app(coding_card, executor=executor)
        discovered = a2a_agent_card_to_discovered_agent(coding_card)

        def _build_request(prompt: str) -> A2ATaskRequest:
            return A2ATaskRequest(
                task_id="t-resume-1",
                context_id="ctx-resume",
                target_agent_id=discovered.agent_id,
                skill="code/documentation",
                user_goal=prompt,
                messages=[
                    A2AMessage(role="user", parts=[A2APart(kind="text", text=prompt)])
                ],
                metadata={},
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=coding_app), base_url=coding_card.url,
        ) as coding_http_client:
            transport = HTTPA2ATransport(
                http_clients_by_origin={_origin(coding_card.url): coding_http_client}
            )
            client = A2AClient(transport)
            first = await client.send_task(
                discovered, _build_request("Document this module and email it.")
            )
            second = await client.send_task(
                discovered, _build_request(second_prompt)
            )

    return (
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
        email_executor,
        completion_calls,
    )


# Email confirmation pause/resume tests removed: the email agent no longer
# requires user confirmation, so send_email_via_directory always completes
# without pausing.


# ---------------------------------------------------------------------------
# Streaming progress events: the agent_loop must call progress_callback after
# each LLM round and tool result, and the heartbeat must fire when an LLM call
# stalls past heartbeat_interval_s. These keep the PI-side SSE stream alive
# under the 30s read timeout.
# ---------------------------------------------------------------------------


def test_progress_callback_receives_round_and_tool_events() -> None:
    from agent_loop import run_agent_loop

    plan = [
        ("tool", "run_coder", {"task": "do work"}),
        ("final", "all done"),
    ]
    completion = _completion_stub(plan)

    async def runner(args):
        return {"submission": "ok", "exit_status": "Submitted", "generated_files": []}

    events: list[dict] = []

    async def progress_callback(event):
        events.append(event)

    trace = asyncio.run(
        run_agent_loop(
            user_prompt="hi",
            model="stub",
            tool_handlers={"run_coder": runner},
            completion_fn=completion,
            step_limit=4,
            time_limit_s=5.0,
            progress_callback=progress_callback,
            heartbeat_interval_s=10.0,  # well above the stub's instant return
        )
    )

    assert trace.exit_reason == "completed"
    kinds = [e["kind"] for e in events]
    # Expect: round_started, tool_call(run_coder), tool_result(run_coder),
    # round_started (round 2), thought(final).
    assert "round_started" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "thought" in kinds
    final_thoughts = [e for e in events if e["kind"] == "thought" and e.get("is_final")]
    assert final_thoughts and final_thoughts[-1]["content"] == "all done"
    tool_call = next(e for e in events if e["kind"] == "tool_call")
    assert tool_call["name"] == "run_coder"
    assert tool_call["args_keys"] == ["task"]


def test_heartbeat_fires_when_llm_round_exceeds_interval() -> None:
    from agent_loop import run_agent_loop

    async def slow_completion(**kwargs):
        await asyncio.sleep(0.4)  # > heartbeat_interval_s below
        return _stub_response(content="finished after slow round")

    events: list[dict] = []

    async def progress_callback(event):
        events.append(event)

    trace = asyncio.run(
        run_agent_loop(
            user_prompt="hi",
            model="stub",
            tool_handlers={},
            completion_fn=slow_completion,
            step_limit=2,
            time_limit_s=5.0,
            progress_callback=progress_callback,
            heartbeat_interval_s=0.1,
        )
    )

    assert trace.exit_reason == "completed"
    heartbeats = [e for e in events if e["kind"] == "still_processing"]
    assert heartbeats, "expected at least one still_processing event"
    assert heartbeats[0]["step"] == 1
    assert heartbeats[0]["elapsed_s"] >= 0.1


def test_executor_emits_status_updates_for_each_progress_event(tmp_path) -> None:
    """End-to-end: the coding agent's executor turns each progress event into
    a TaskStatusUpdateEvent on the SDK event queue, which streams to the PI."""
    from a2a.types import TaskState
    from a2a.server.events import EventQueue
    from a2a.server.tasks import TaskUpdater

    captured: list[dict] = []

    async def fake_callback(event):
        captured.append(event)

    queue = EventQueue()
    updater = TaskUpdater(queue, "task-x", "ctx-x")
    callback = coding_agent._build_progress_callback(updater)

    async def drive():
        await callback({"kind": "round_started", "step": 1})
        await callback({"kind": "thought", "step": 1, "content": "thinking out loud", "is_final": False})
        await callback({"kind": "tool_call", "step": 1, "name": "run_coder", "args_keys": ["task"]})
        await callback({"kind": "tool_result", "step": 1, "name": "run_coder", "result_keys": ["submission"], "error": None})
        await callback({"kind": "still_processing", "step": 2, "elapsed_s": 30.0})

    asyncio.run(drive())

    # Drain the event queue and inspect the status updates.
    drained: list = []

    async def drain():
        while True:
            try:
                drained.append(await asyncio.wait_for(queue.dequeue_event(), timeout=0.05))
            except asyncio.TimeoutError:
                return
        await queue.close()

    asyncio.run(drain())

    # Each progress event becomes one TaskStatusUpdateEvent in working state.
    assert len(drained) == 5
    for event in drained:
        assert event.status.state == TaskState.working
        assert event.final is False
        assert event.status.message is not None
        text = event.status.message.parts[0].root.text
        assert text  # non-empty user-visible string
    # The "still_processing" event should mention the elapsed seconds.
    still = drained[-1]
    assert "still processing" in still.status.message.parts[0].root.text
    assert "30.0" in still.status.message.parts[0].root.text


# ---------------------------------------------------------------------------
# dirctl-backed directory: discovery + push/publish/unpublish
# ---------------------------------------------------------------------------


import subprocess


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capture_subprocess_calls(monkeypatch, responses):
    """Patch subprocess.run to return queued responses and record argv."""
    calls: list[list[str]] = []
    queue = list(responses)

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if not queue:
            raise AssertionError(f"Unexpected dirctl call: {cmd}")
        return queue.pop(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_dirctl_directory_search_filters_by_a2a_label_and_skill_tag(monkeypatch) -> None:
    list_response = json.dumps(
        [
            {
                "record_ref": {"cid": "cid-email"},
                "labels": [
                    "/skills/foo",
                    "/modules/integration/a2a",
                ],
            },
            {
                "record_ref": {"cid": "cid-not-a2a"},
                "labels": ["/skills/bar"],
            },
            {
                "record_ref": {"cid": "cid-other-a2a"},
                "labels": ["/modules/integration/a2a"],
            },
        ]
    )
    pull_email = json.dumps(
        {
            "name": "https://example.invalid/cspl-email-agent",
            "description": "Gmail A2A agent.",
            "annotations": {
                "a2a.url": "http://10.229.117.223:9103/",
                "agentic_network.agent_id": "email-agent",
                "agentic_network.display_name": "Email Agent",
                "agentic_network.organization": "CSPL Email Agent",
                "agentic_network.custom_skill_tags": "email,email/send,email/draft",
            },
            "modules": [],
            "locators": [],
        }
    )
    pull_other = json.dumps(
        {
            "name": "other",
            "annotations": {
                "a2a.url": "http://10.0.0.1:9999/",
                "agentic_network.custom_skill_tags": "weather/forecast",
            },
        }
    )
    calls = _capture_subprocess_calls(
        monkeypatch,
        [
            _FakeCompletedProcess(0, list_response, ""),
            _FakeCompletedProcess(0, pull_email, ""),
            _FakeCompletedProcess(0, pull_other, ""),
        ],
    )

    directory = coding_agent._DirctlDirectory(
        server_addr="10.229.117.37:8888",
        dirctl_path="/usr/local/bin/dirctl",
    )
    matches = directory.search_agents(skill="email/send")

    assert [a.agent_id for a in matches] == ["email-agent"]
    assert matches[0].endpoint == "http://10.229.117.223:9103/"
    assert matches[0].source_record_id == "cid-email"

    # Verified argv: routing list once, then pull each A2A candidate.
    assert calls[0] == [
        "/usr/local/bin/dirctl", "--server-addr", "10.229.117.37:8888",
        "routing", "list", "--output", "json",
    ]
    assert calls[1][:5] == [
        "/usr/local/bin/dirctl", "--server-addr", "10.229.117.37:8888",
        "pull", "cid-email",
    ]
    assert calls[2][4] == "cid-other-a2a"
    # Non-A2A label was never pulled.
    assert all("cid-not-a2a" not in argv for argv in calls)


def test_dirctl_directory_matches_skill_via_card_data_when_annotation_missing(
    monkeypatch,
) -> None:
    list_response = json.dumps(
        [{"record_ref": {"cid": "cid-x"}, "labels": ["/modules/integration/a2a"]}]
    )
    pull_response = json.dumps(
        {
            "name": "third-party",
            "annotations": {"a2a.url": "http://10.0.0.5:9000/"},
            "modules": [
                {
                    "name": "integration/a2a",
                    "data": {
                        "card_data": {
                            "skills": [{"id": "email/send"}, {"id": "email/draft"}]
                        }
                    },
                }
            ],
        }
    )
    _capture_subprocess_calls(
        monkeypatch,
        [
            _FakeCompletedProcess(0, list_response, ""),
            _FakeCompletedProcess(0, pull_response, ""),
        ],
    )
    directory = coding_agent._DirctlDirectory(server_addr="addr:8888")
    matches = directory.search_agents(skill="email/send")
    assert len(matches) == 1
    assert matches[0].endpoint == "http://10.0.0.5:9000/"


def test_dirctl_directory_propagates_nonzero_exit(monkeypatch) -> None:
    _capture_subprocess_calls(
        monkeypatch,
        [_FakeCompletedProcess(1, "", "permission denied")],
    )
    directory = coding_agent._DirctlDirectory(server_addr="addr:8888")
    with pytest.raises(coding_agent._DirctlError) as info:
        directory.list_records()
    assert "permission denied" in str(info.value)


def test_dirctl_directory_push_returns_cid(monkeypatch) -> None:
    calls = _capture_subprocess_calls(
        monkeypatch,
        [_FakeCompletedProcess(0, "baeareiabcdef\n", "")],
    )
    directory = coding_agent._DirctlDirectory(server_addr="addr:8888")
    cid = directory.push(Path("/tmp/record.json"))
    assert cid == "baeareiabcdef"
    assert calls[0][-3:] == ["/tmp/record.json", "--output", "raw"]


def test_dirctl_directory_publish_and_unpublish(monkeypatch) -> None:
    calls = _capture_subprocess_calls(
        monkeypatch,
        [
            _FakeCompletedProcess(0, "", ""),
            _FakeCompletedProcess(0, "", ""),
        ],
    )
    directory = coding_agent._DirctlDirectory(server_addr="addr:8888")
    directory.publish("baeareiabcdef")
    directory.unpublish("baeareiabcdef")
    assert calls[0][-3:] == ["routing", "publish", "baeareiabcdef"]
    assert calls[1][-3:] == ["routing", "unpublish", "baeareiabcdef"]


def test_dirctl_directory_translates_missing_binary(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("dirctl not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    directory = coding_agent._DirctlDirectory(server_addr="addr:8888")
    with pytest.raises(coding_agent._DirctlError) as info:
        directory.list_records()
    assert "not found on PATH" in str(info.value)


def test_publish_self_to_ads_pushes_record_then_publishes(monkeypatch, tmp_path) -> None:
    calls = _capture_subprocess_calls(
        monkeypatch,
        [
            _FakeCompletedProcess(0, "baeareixxxx\n", ""),
            _FakeCompletedProcess(0, "", ""),
        ],
    )
    cid = coding_agent._publish_self_to_ads(
        CODING_CARD,
        server_addr="addr:8888",
        dirctl_path="dirctl",
    )
    assert cid == "baeareixxxx"
    assert calls[0][3] == "push"
    assert calls[0][-2:] == ["--output", "raw"]
    assert calls[1][-3:] == ["routing", "publish", "baeareixxxx"]
    # The temp record file should have been cleaned up.
    pushed_path = Path(calls[0][4])
    assert not pushed_path.exists()


def test_unpublish_self_from_ads_calls_routing_unpublish(monkeypatch) -> None:
    calls = _capture_subprocess_calls(monkeypatch, [_FakeCompletedProcess(0, "", "")])
    coding_agent._unpublish_self_from_ads(
        "addr:8888", "baeareiyyyy", dirctl_path="dirctl",
    )
    assert calls[0][-3:] == ["routing", "unpublish", "baeareiyyyy"]


def test_build_directory_client_picks_dirctl_when_url_set(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_NETWORK_FAKE_ADS", raising=False)
    monkeypatch.delenv("CODING_AGENT_DIRECTORY_URL", raising=False)
    monkeypatch.delenv("ADS_URL", raising=False)
    client = coding_agent._build_directory_client(
        {"directory_url": "10.229.117.37:8888"}
    )
    assert isinstance(client, coding_agent._DirctlDirectory)
    assert client.server_addr == "10.229.117.37:8888"


def test_build_directory_client_picks_fake_when_requested(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_NETWORK_FAKE_ADS", raising=False)
    client = coding_agent._build_directory_client({"fake_ads": True})
    from v2_agent_network.services.directory.client import DirectoryClient

    assert isinstance(client, DirectoryClient)
    assert client.fake_ads is True


def test_build_directory_client_raises_when_no_directory_configured(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_NETWORK_FAKE_ADS", raising=False)
    monkeypatch.delenv("CODING_AGENT_DIRECTORY_URL", raising=False)
    monkeypatch.delenv("ADS_URL", raising=False)
    with pytest.raises(RuntimeError, match="No directory configured"):
        coding_agent._build_directory_client({})


def test_delivery_email_body_format_includes_paths_and_content() -> None:
    body = coding_agent._format_email_body(
        intro="Custom intro.",
        generated_files=[
            {
                "path": "src/foo.py",
                "action": "created",
                "size_bytes": 17,
                "content": "def foo():\n    pass\n",
            },
            {
                "path": "big.txt",
                "action": "created",
                "size_bytes": 999_999,
                "content_truncated": True,
                "sha256": "deadbeef",
            },
        ],
    )
    assert "Custom intro." in body
    assert "=== src/foo.py (created, 17 bytes) ===" in body
    assert "def foo():" in body
    assert "=== big.txt" in body
    assert "deadbeef" in body
    assert "truncated" in body
