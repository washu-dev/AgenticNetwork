#!/usr/bin/env python3
"""mini-swe-agent A2A wrapper for the AgenticNetwork.

This script exposes ``mini-swe-agent`` (https://github.com/SWE-agent/mini-swe-agent)
as an A2A HTTP service so the AgenticNetwork PI agent (or any other A2A
client) can discover the coding agent through ADS and invoke it remotely.

Examples:
  export CODING_AGENT_MODEL=anthropic/claude-sonnet-4-5-20250929
  export ANTHROPIC_API_KEY=sk-...

  # Start the A2A server on port 9106 (matches the published Agent Card).
  python coding_agent.py serve-a2a --host 0.0.0.0 --port 9106

  # Run a single skill from the CLI (no A2A round-trip).
  python coding_agent.py run-skill code/repository_inspection \\
      --prompt "Review this repo." \\
      --input-file local.py:src/foo.py

  # Print every skill the executor handles.
  python coding_agent.py tools

WORKSPACE & FILE RETURN CONTRACT
--------------------------------
Every A2A request runs inside a freshly-created **scratch workspace**.  The
host filesystem outside the workspace is never touched, so a remote caller can
ask the coding agent to "write me a script that does X" and get the source
code back in the response, instead of having the file land on whatever machine
the agent happens to run on.

Two flavours of source input are accepted on the request:

* ``metadata.source_files = [{"path": "src/foo.py", "content": "..."}]`` — a
  simple inline list. ``path`` must be a workspace-relative path (no ``..``,
  no absolute paths).
* A2A ``FilePart`` parts on the message — ``file.bytes`` (base64) or
  ``file.uri`` plus ``file.name`` for the workspace-relative path.

After the runner finishes, the workspace is snapshotted and the diff against
the seeded state is returned in ``structured_output.generated_files``::

    [{"path": "src/foo.py",
      "action": "created" | "modified" | "deleted",
      "size_bytes": 1234,
      "sha256": "...",
      "content": "...",                 # text only, omitted if binary or > 256 KB
      "content_truncated": false}]

The scratch workspace is removed after the response is sent unless
``--keep-workspace`` is passed (handy for debugging).

YOLO MODE
---------
This wrapper runs ``mini-swe-agent``'s ``DefaultAgent``. ``DefaultAgent`` has
no human-in-the-loop confirmation step: every bash command the model emits is
executed by ``LocalEnvironment`` via ``subprocess.run`` immediately. The
workspace boundary contains the blast radius of those commands, but the
service should still be deployed inside a container or restricted runner.

The PI agent's policy still gates write skills (``code/bug_fix``,
``code/test_generation``) with ``requires_confirmation`` *before* the request
reaches this server.

Configuration:
  CODING_AGENT_MODEL              required, e.g. anthropic/claude-sonnet-4-5-20250929
  MSWEA_MODEL_NAME                alternative to CODING_AGENT_MODEL
  CODING_AGENT_WORKSPACE_ROOT     parent dir for scratch workspaces (default: tempfile)
  MSWEA_SILENT_STARTUP=1          suppresses mini-swe-agent's import banner
  MSWEA_COST_TRACKING=ignore_errors recommended unless your model has cost metadata

The Agent Card describing this service lives in
``AgenticNetwork/v2_agent_network/configs/agent_cards/coding_agent.card.json``.
Pass ``--card`` to override.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

logger = logging.getLogger("coding_agent")

if TYPE_CHECKING:
    from a2a.server.agent_execution import RequestContext as A2ARequestContext
    from a2a.server.events import EventQueue as A2AEventQueue
    from a2a.server.tasks import TaskUpdater as A2ATaskUpdater
    from a2a.types import AgentCard as A2AAgentCard

try:
    from a2a.server.agent_execution import AgentExecutor as _AgentExecutorBase
    from a2a.server.apps import A2AStarletteApplication as _A2AStarletteApplication
    from a2a.server.request_handlers import DefaultRequestHandler as _DefaultRequestHandler
    from a2a.server.tasks import InMemoryTaskStore as _InMemoryTaskStore
    from a2a.server.tasks import TaskUpdater as _TaskUpdater
    from a2a.types import AgentCard as _AgentCard
    from a2a.types import DataPart as _DataPart
    from a2a.types import FilePart as _FilePart
    from a2a.types import Part as _Part
    from a2a.types import TextPart as _TextPart
except ImportError as exc:
    A2A_IMPORT_ERROR: ImportError | None = exc
    _AgentExecutorBase = object
    _A2AStarletteApplication = None
    _DefaultRequestHandler = None
    _InMemoryTaskStore = None
    _TaskUpdater = None
    _AgentCard = None
    _DataPart = None
    _FilePart = None
    _Part = None
    _TextPart = None
else:
    A2A_IMPORT_ERROR = None


DEFAULT_CODING_A2A_CARD = (
    Path(__file__).resolve().parents[2]
    / "v2_agent_network"
    / "configs"
    / "agent_cards"
    / "coding_agent.card.json"
)

SKILL_REPOSITORY_INSPECTION = "code/repository_inspection"
SKILL_BUG_FIX = "code/bug_fix"
SKILL_PR_REVIEW = "code/pr_review"
SKILL_TEST_GENERATION = "code/test_generation"
SKILL_DOCUMENTATION = "code/documentation"

WRITE_GATED_SKILLS: set[str] = {SKILL_BUG_FIX, SKILL_TEST_GENERATION}

_READ_ONLY_LIMITS: dict[str, Any] = {"step_limit": 12, "cost_limit": 0.50}
_WRITE_LIMITS: dict[str, Any] = {"step_limit": 25, "cost_limit": 1.50}

_ARTIFACT_IDS: dict[str, tuple[str, str]] = {
    SKILL_REPOSITORY_INSPECTION: ("coding-repository-inspection", "coding_repository_inspection"),
    SKILL_BUG_FIX: ("coding-bug-fix-plan", "coding_bug_fix_plan"),
    SKILL_PR_REVIEW: ("coding-pr-review", "coding_pr_review"),
    SKILL_TEST_GENERATION: ("coding-test-generation-plan", "coding_test_generation_plan"),
    SKILL_DOCUMENTATION: ("coding-documentation", "coding_documentation"),
}

# Inline content cap for returned files. Larger files are returned with
# ``content_truncated=True`` and ``content`` omitted; the caller can ask the
# service for them by URI/artifact reference in a follow-up (not implemented).
_INLINE_FILE_BYTE_LIMIT = 256 * 1024


AgentRunner = Callable[..., dict[str, Any]]


class CodingAgentExecutor(_AgentExecutorBase):
    """A2A executor that delegates code skills to mini-swe-agent's DefaultAgent.

    Each request runs in a freshly-created scratch workspace. The host
    filesystem outside the workspace is not touched. Source files supplied on
    the A2A request are seeded into the workspace before the runner starts;
    files created or modified by the runner are returned to the caller in
    ``structured_output.generated_files``.

    Parameters
    ----------
    agent_runner:
        Optional callable used to run a single coding task. Must accept the
        keyword arguments ``task``, ``skill``, ``workspace``, ``limits`` and
        return a dict with at least ``submission`` (str), ``exit_status`` (str),
        ``n_calls`` (int), ``cost`` (float), and ``messages`` (list). When
        omitted, the default ``mini-swe-agent`` runner is used.
    workspace_root:
        Parent directory under which per-request scratch workspaces are
        created. Defaults to ``CODING_AGENT_WORKSPACE_ROOT`` or the OS tempdir.
    cleanup:
        When True (the default), the scratch workspace is removed after the
        response is emitted. Set to False for debugging.
    delivery_directory:
        Optional ``v2_agent_network.services.directory.client.DirectoryClient``
        used to discover the email agent for ``delivery`` requests. Tests
        inject one wired to a fake ADS holding the email mock's card. When
        omitted, the executor builds one per request from the delivery
        metadata + ``CODING_AGENT_DIRECTORY_URL`` / ``ADS_URL`` /
        ``AGENT_NETWORK_FAKE_ADS`` env vars.
    delivery_transport:
        Optional ``v2_agent_network.services.a2a.transport.A2ATransport`` used
        to call the discovered email agent. Tests inject one wired to an
        in-process email mock; production uses ``HTTPA2ATransport()`` with a
        real httpx client per call.
    """

    def __init__(
        self,
        agent_runner: AgentRunner | None = None,
        workspace_root: str | None = None,
        cleanup: bool = True,
        delivery_directory: Any | None = None,
        delivery_transport: Any | None = None,
        completion_fn: Any | None = None,
        agent_step_limit: int = 4,
        agent_time_limit_s: float = 900.0,
        state_store: Any | None = None,
    ) -> None:
        if A2A_IMPORT_ERROR is not None:
            raise RuntimeError(
                "The a2a-sdk package is required for the coding agent. "
                "Install AgenticNetwork dependencies first."
            ) from A2A_IMPORT_ERROR
        self._runner: AgentRunner = agent_runner or _default_minisweagent_runner
        env_root = os.getenv("CODING_AGENT_WORKSPACE_ROOT")
        self._workspace_root = (
            Path(workspace_root or env_root or tempfile.gettempdir()).resolve()
        )
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._cleanup = cleanup
        self._delivery_directory = delivery_directory
        self._delivery_transport = delivery_transport
        self._completion_fn = completion_fn
        self._agent_step_limit = agent_step_limit
        self._agent_time_limit_s = agent_time_limit_s
        if state_store is None:
            from state_store import CodingTaskStateStore
            state_store = CodingTaskStateStore(self._workspace_root / "_state")
        self._state_store = state_store

    async def execute(
        self, context: A2ARequestContext, event_queue: A2AEventQueue
    ) -> None:
        from agent_loop import (
            AgentTrace,
            deserialize_invocations,
            run_agent_loop,
            serialize_invocations,
        )
        from state_store import CodingTaskState

        updater = _task_updater(event_queue, context.task_id, context.context_id)
        prompt = _user_text(context)
        prompt_preview = prompt.replace("\n", " ")[:120]

        logger.info(
            "request received: task_id=%s context_id=%s prompt=%r",
            context.task_id, context.context_id, prompt_preview,
        )

        await updater.start_work()
        request_started = time.monotonic()

        # State is keyed by context_id (the conversation thread), not task_id:
        # the installed a2a-sdk generates a fresh task_id per ``message/send``
        # call, so task_id is not stable across a pause/resume round-trip,
        # but context_id is.
        state_key = context.context_id
        existing = self._state_store.load(state_key)
        is_resume = (
            existing is not None
            and existing.status == "input_required"
            and existing.pending is not None
        )

        if is_resume:
            assert existing is not None and existing.pending is not None
            workspace = Path(existing.workspace)
            if not workspace.exists():
                logger.warning(
                    "resume requested but workspace missing: context_id=%s expected=%s",
                    state_key, workspace,
                )
                self._state_store.delete(state_key)
                await _emit_failure(
                    updater, "agent_loop", context, workspace,
                    ValueError("Cannot resume task: workspace was cleaned up"),
                )
                return
            seeded = list(existing.seeded_files)
            initial_snapshot = dict(existing.initial_snapshot)
            original_user_prompt = existing.user_prompt
            initial_messages = list(existing.messages)
            initial_messages.append({
                "role": "tool",
                "tool_call_id": existing.pending.get("tool_call_id"),
                "name": existing.pending.get("tool_name") or "send_email_via_directory",
                "content": json.dumps({
                    "user_response": prompt,
                    "guidance": (
                        "The user replied to your last send_email_via_directory "
                        "pause. If they approved, retry send_email_via_directory "
                        "with the same args plus confirmed=true. If they "
                        "declined, do NOT retry — reply with a short final "
                        "summary acknowledging that the files were generated "
                        "but not emailed, and stop."
                    ),
                }),
            })
            initial_invocations = deserialize_invocations(existing.tool_invocations)
            initial_n_steps = existing.n_steps
            delivery_results: list[dict[str, Any]] = list(existing.delivery_results)
            run_coder_calls: list[dict[str, Any]] = list(existing.run_coder_calls)
            existing.n_continuations += 1
            existing.task_id = context.task_id
            state = existing
            logger.info(
                "resuming task: context_id=%s task_id=%s n_continuations=%d "
                "pending_tool=%s reply=%r",
                state_key, context.task_id, state.n_continuations,
                existing.pending.get("tool_name"), prompt[:120],
            )
        else:
            if existing is not None:
                # Stale state for a completed task — wipe before reusing the slot.
                self._state_store.delete(state_key)
            workspace = self._workspace_root / f"context-{state_key}"
            workspace.mkdir(parents=True, exist_ok=True)
            try:
                seeded = _seed_workspace_from_request(workspace, context)
            except ValueError as exc:
                logger.warning(
                    "workspace seeding rejected: task_id=%s error=%s",
                    context.task_id, exc,
                )
                await _emit_failure(updater, "agent_loop", context, workspace, exc)
                return
            logger.info(
                "workspace ready: task_id=%s path=%s seeded_files=%d",
                context.task_id, workspace, len(seeded),
            )
            initial_snapshot = _snapshot_workspace(workspace)
            initial_messages = None
            initial_invocations = None
            initial_n_steps = 0
            original_user_prompt = prompt
            delivery_results = []
            run_coder_calls = []
            state = CodingTaskState(
                state_key=state_key,
                task_id=context.task_id,
                context_id=context.context_id,
                user_prompt=prompt,
                workspace=str(workspace),
                seeded_files=seeded,
                initial_snapshot=initial_snapshot,
                started_at=request_started,
            )

        tool_handlers = self._build_tool_handlers(
            workspace=workspace,
            user_prompt=original_user_prompt,
            context_id=context.context_id,
            run_coder_calls=run_coder_calls,
            delivery_results=delivery_results,
        )

        model_name = (
            os.getenv("CODING_AGENT_PLANNER_MODEL")
            or os.getenv("CODING_AGENT_MODEL")
            or os.getenv("MSWEA_MODEL_NAME")
            or "openai/gpt-5-mini"
        )

        progress_callback = _build_progress_callback(updater)

        try:
            trace: AgentTrace = await run_agent_loop(
                user_prompt=original_user_prompt,
                model=model_name,
                tool_handlers=tool_handlers,
                completion_fn=self._completion_fn,
                step_limit=self._agent_step_limit,
                time_limit_s=self._agent_time_limit_s,
                initial_messages=initial_messages,
                initial_tool_invocations=initial_invocations,
                initial_n_steps=initial_n_steps,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            logger.exception(
                "agent loop crashed: task_id=%s elapsed=%.2fs",
                context.task_id, time.monotonic() - request_started,
            )
            self._state_store.delete(state_key)
            if self._cleanup:
                shutil.rmtree(workspace, ignore_errors=True)
            await _emit_failure(updater, "agent_loop", context, workspace, exc)
            return

        final_snapshot = _snapshot_workspace(workspace)
        aggregate_files = _capture_workspace_changes(
            workspace, initial_snapshot, final_snapshot
        )
        logger.info(
            "agent loop finished: task_id=%s exit=%s steps=%d tools=%d "
            "elapsed=%.2fs aggregate_files=%d",
            context.task_id,
            trace.exit_reason, trace.n_steps, len(trace.tool_calls),
            trace.elapsed, len(aggregate_files),
        )

        output: dict[str, Any] = {
            "user_prompt": original_user_prompt,
            "context_id": context.context_id,
            "task_id": context.task_id,
            "workspace": str(workspace),
            "seeded_files": seeded,
            "generated_files": aggregate_files,
            "delivery_results": delivery_results,
            "agent": {
                "final_message": trace.final_message,
                "exit_reason": trace.exit_reason,
                "n_steps": trace.n_steps,
                "elapsed_s": trace.elapsed,
                "n_continuations": state.n_continuations,
                "tool_calls": [
                    {
                        "name": tc.name,
                        "args_keys": sorted(tc.args.keys()),
                        "result_keys": sorted((tc.result or {}).keys()) if tc.result else [],
                        "error": tc.error,
                    }
                    for tc in trace.tool_calls
                ],
                "run_coder_calls": run_coder_calls,
                "pending": trace.pending,
            },
        }

        await updater.add_artifact(
            [_data_part(output)],
            artifact_id="coding-agent-result",
            name="coding_agent_result",
            metadata={"content_type": "application/json"},
        )

        if trace.exit_reason == "input_required" and trace.pending:
            state.messages = trace.messages
            state.tool_invocations = serialize_invocations(trace.tool_calls)
            state.n_steps = trace.n_steps
            state.delivery_results = delivery_results
            state.run_coder_calls = run_coder_calls
            state.pending = trace.pending
            state.status = "input_required"
            state.last_updated = time.monotonic()
            self._state_store.save(state)
            prompt_text = trace.pending.get("prompt") or "Awaiting your confirmation."
            message = updater.new_agent_message(
                [_text_part(prompt_text)],
                metadata={"structured_output": output},
            )
            await updater.requires_input(message=message)
            logger.info(
                "request paused for confirmation: task_id=%s pending_tool=%s",
                context.task_id, trace.pending.get("tool_name"),
            )
            return

        summary = trace.final_message or _autosummary(
            aggregate_files, delivery_results, trace.exit_reason
        )
        message = updater.new_agent_message(
            [_text_part(summary)],
            metadata={"structured_output": output},
        )
        await updater.complete(message=message)
        self._state_store.delete(state_key)
        if self._cleanup:
            shutil.rmtree(workspace, ignore_errors=True)
            logger.debug("workspace cleaned: context_id=%s", state_key)
        else:
            logger.info(
                "workspace retained for debugging: task_id=%s path=%s",
                context.task_id, workspace,
            )
        logger.info(
            "request done: task_id=%s state=completed total_elapsed=%.2fs",
            context.task_id, time.monotonic() - request_started,
        )

    def _build_tool_handlers(
        self,
        *,
        workspace: Path,
        user_prompt: str,
        context_id: str,
        run_coder_calls: list[dict[str, Any]],
        delivery_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        runner_limits = {"step_limit": 6, "cost_limit": 1.50}

        async def _seed_extra_files(files: Any) -> list[str]:
            written: list[str] = []
            if not isinstance(files, list):
                return written
            for entry in files:
                if not isinstance(entry, dict):
                    continue
                path = entry.get("path")
                content = entry.get("content")
                if not isinstance(path, str) or not isinstance(content, str):
                    continue
                try:
                    target = _safe_workspace_path(workspace, path)
                except ValueError:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                written.append(path)
            return written

        async def tool_run_coder(args: Any) -> dict[str, Any]:
            task_text = str((args or {}).get("task") or "").strip()
            if not task_text:
                return {"error": "run_coder requires non-empty 'task'"}
            framed_task = (
                "FINISH RULE (read first): When the work is done, your VERY NEXT "
                "command must be exactly `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` "
                "on its own — no `&&`, no chaining, no leading/trailing commands. "
                "The harness only detects submission when this string is the FIRST "
                "LINE of stdout. If you chain it, the run is wasted and you will "
                "loop until the step limit.\n\n"
                f"TASK:\n{task_text}"
            )
            extra = await _seed_extra_files((args or {}).get("files"))
            before = _snapshot_workspace(workspace)
            runner_started = time.monotonic()
            try:
                run_result = await asyncio.to_thread(
                    self._runner,
                    task=framed_task,
                    skill="agent_loop/run_coder",
                    workspace=str(workspace),
                    limits=runner_limits,
                )
            except Exception as exc:
                logger.exception("run_coder runner crashed")
                run_coder_calls.append({
                    "task": task_text[:200],
                    "seeded_extra": extra,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                return {"error": f"{type(exc).__name__}: {exc}"}
            after = _snapshot_workspace(workspace)
            generated = _capture_workspace_changes(workspace, before, after)
            elapsed = time.monotonic() - runner_started
            run_coder_calls.append({
                "task": task_text[:200],
                "seeded_extra": extra,
                "exit_status": run_result.get("exit_status"),
                "n_calls": run_result.get("n_calls"),
                "cost": run_result.get("cost"),
                "elapsed_s": elapsed,
                "generated_paths": [f.get("path") for f in generated],
            })
            logger.info(
                "run_coder call: paths=%s exit=%s n_calls=%s cost=%s elapsed=%.2fs",
                [f.get("path") for f in generated],
                run_result.get("exit_status"),
                run_result.get("n_calls"),
                run_result.get("cost"),
                elapsed,
            )
            return {
                "submission": run_result.get("submission") or "",
                "exit_status": run_result.get("exit_status"),
                "n_calls": run_result.get("n_calls"),
                "cost": run_result.get("cost"),
                "generated_files": generated,
            }

        async def tool_discover_agents(args: Any) -> dict[str, Any]:
            skill_query = str((args or {}).get("skill_query") or "").strip()
            if not skill_query:
                return {"error": "discover_agents requires 'skill_query'"}
            try:
                client = self._delivery_directory or _build_directory_client({})
            except Exception as exc:
                return {
                    "error": f"directory_unavailable: {type(exc).__name__}: {exc}",
                    "skill_query": skill_query,
                }
            try:
                matches = await asyncio.to_thread(client.search_agents, skill=skill_query)
            except Exception as exc:
                return {
                    "error": f"discovery_failed: {type(exc).__name__}: {exc}",
                    "skill_query": skill_query,
                }
            agents = [
                {
                    "agent_id": getattr(a, "agent_id", None),
                    "name": getattr(a, "name", None),
                    "endpoint": getattr(a, "endpoint", None),
                    "description": getattr(a, "description", None),
                    "skills": [
                        getattr(s, "name", None) or getattr(s, "id", None) or str(s)
                        for s in (getattr(a, "skills", None) or [])
                    ],
                }
                for a in (matches or [])
            ]
            logger.info(
                "discover_agents: skill_query=%s matched=%d", skill_query, len(agents),
            )
            return {"skill_query": skill_query, "agents": agents}

        async def tool_send_email(args: Any) -> dict[str, Any]:
            args = args or {}
            to = args.get("to") or []
            if not isinstance(to, list) or not all(isinstance(x, str) for x in to):
                return {"error": "send_email_via_directory requires 'to' as a list of strings"}
            files = args.get("files") or []
            normalized: list[dict[str, Any]] = []
            if isinstance(files, list):
                for entry in files:
                    if not isinstance(entry, dict):
                        continue
                    path = entry.get("path")
                    content = entry.get("content")
                    if not isinstance(path, str) or not isinstance(content, str):
                        continue
                    normalized.append({
                        "path": path,
                        "action": entry.get("action") or "created",
                        "size_bytes": len(content.encode("utf-8")),
                        "content": content,
                    })
            delivery = {
                "kind": "email",
                "to": to,
                "cc": args.get("cc") or [],
                "subject": args.get("subject") or "Coding agent output",
                "body": args.get("body") or "",
            }
            if not normalized:
                # _attempt_delivery returns no_files for empty; surface that explicitly.
                logger.info("send_email_via_directory: no files supplied; calling anyway")
            # Email agent no longer requires user confirmation — always send confirmed.
            result = await _attempt_delivery(
                delivery=delivery,
                skill="agent_loop",
                user_prompt=user_prompt,
                generated_files=normalized,
                context_id=context_id,
                directory=self._delivery_directory,
                transport=self._delivery_transport,
                confirmed=True,
            )
            delivery_results.append(result)
            logger.info(
                "send_email_via_directory result: status=%s to=%s",
                result.get("status"), to,
            )
            return result

        return {
            "run_coder": tool_run_coder,
            "discover_agents": tool_discover_agents,
            "send_email_via_directory": tool_send_email,
        }

    async def cancel(
        self, context: A2ARequestContext, event_queue: A2AEventQueue
    ) -> None:
        updater = _task_updater(event_queue, context.task_id, context.context_id)
        await updater.reject()


def load_coding_a2a_agent_card(
    card_path: Path | str = DEFAULT_CODING_A2A_CARD,
) -> A2AAgentCard:
    _require_a2a()
    assert _AgentCard is not None
    with Path(card_path).open("r", encoding="utf-8") as fh:
        return _AgentCard.model_validate(json.load(fh))


def create_coding_a2a_app(
    agent_card: A2AAgentCard,
    executor: CodingAgentExecutor | None = None,
) -> Any:
    _require_a2a()
    assert _A2AStarletteApplication is not None
    assert _DefaultRequestHandler is not None
    assert _InMemoryTaskStore is not None
    request_handler = _DefaultRequestHandler(
        agent_executor=executor or CodingAgentExecutor(),
        task_store=_InMemoryTaskStore(),
    )
    return _A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()


def _require_a2a() -> None:
    if A2A_IMPORT_ERROR is not None:
        raise RuntimeError(
            "A2A server mode requires the a2a-sdk, starlette, and uvicorn packages. "
            "Install the AgenticNetwork dependencies before running serve-a2a."
        ) from A2A_IMPORT_ERROR


def _task_updater(
    event_queue: A2AEventQueue,
    task_id: str,
    context_id: str,
) -> A2ATaskUpdater:
    _require_a2a()
    assert _TaskUpdater is not None
    return _TaskUpdater(event_queue, task_id, context_id)


def _format_progress_event(event: dict[str, Any]) -> str | None:
    """Render an agent_loop progress event as a one-line update for the PI agent.

    Returns ``None`` when the event isn't worth surfacing (keeps the SSE stream
    unchatty). Each non-None return becomes a working-state status update so the
    PI agent's HTTP read timeout never trips on long LLM rounds.
    """
    kind = event.get("kind")
    step = event.get("step")
    if kind == "round_started":
        return f"step {step}: thinking..."
    if kind == "still_processing":
        return f"step {step}: still processing ({event.get('elapsed_s')}s)"
    if kind == "thought":
        text = (event.get("content") or "").strip()
        if not text:
            return None
        snippet = text.splitlines()[0]
        if len(snippet) > 240:
            snippet = snippet[:237] + "..."
        prefix = "step %s: " % step
        return prefix + snippet
    if kind == "tool_call":
        keys = ",".join(event.get("args_keys") or [])
        return f"step {step}: calling {event.get('name')}({keys})"
    if kind == "tool_result":
        err = event.get("error")
        if err:
            return f"step {step}: {event.get('name')} error: {err}"
        keys = ",".join(event.get("result_keys") or [])
        return f"step {step}: {event.get('name')} done ({keys})"
    return None


def _build_progress_callback(updater: A2ATaskUpdater) -> Any:
    """Wrap the executor's TaskUpdater into an agent_loop progress callback.

    Each event becomes a ``working``-state status update, which the SDK
    enqueues as a ``TaskStatusUpdateEvent``. When the PI agent talks to us
    over ``message/stream`` (SSE), each event flushes immediately, so the
    coding agent's reasoning steps and heartbeats arrive at the PI agent
    in real time and the 30s read timeout never trips.
    """
    from a2a.types import TaskState

    async def callback(event: dict[str, Any]) -> None:
        text = _format_progress_event(event)
        if text is None:
            return
        message = updater.new_agent_message(
            [_text_part(text)],
            metadata={"progress_event": event},
        )
        await updater.update_status(TaskState.working, message=message, final=False)

    return callback


def _autosummary(
    aggregate_files: list[dict[str, Any]],
    delivery_results: list[dict[str, Any]],
    exit_reason: str,
) -> str:
    """Fallback text summary when the agent loop ended without a final message."""
    parts: list[str] = []
    if aggregate_files:
        paths = [f.get("path") for f in aggregate_files[:5]]
        suffix = f" (+{len(aggregate_files) - 5} more)" if len(aggregate_files) > 5 else ""
        parts.append(f"generated {len(aggregate_files)} file(s): {paths}{suffix}")
    else:
        parts.append("no files generated")
    if delivery_results:
        statuses = [r.get("status") for r in delivery_results]
        parts.append(f"delivery: {statuses}")
    parts.append(f"exit_reason={exit_reason}")
    return "; ".join(parts)


def _data_part(data: dict[str, Any]) -> Any:
    _require_a2a()
    assert _DataPart is not None
    assert _Part is not None
    return _Part(root=_DataPart(data=data))


def _text_part(text: str) -> Any:
    _require_a2a()
    assert _Part is not None
    assert _TextPart is not None
    return _Part(root=_TextPart(text=text))


def _is_data_part(value: Any) -> bool:
    return _DataPart is not None and isinstance(value, _DataPart)


def _is_file_part(value: Any) -> bool:
    return _FilePart is not None and isinstance(value, _FilePart)


def _skill_from_context(context: A2ARequestContext) -> str:
    metadata = context.metadata or {}
    return str(metadata.get("skill") or SKILL_REPOSITORY_INSPECTION)


def _user_text(context: A2ARequestContext) -> str:
    try:
        return str(context.get_user_input())
    except Exception:
        return ""


def _limits_for_skill(skill: str) -> dict[str, Any]:
    return _WRITE_LIMITS if skill in WRITE_GATED_SKILLS else _READ_ONLY_LIMITS


# ---------------------------------------------------------------------------
# Workspace seeding & change capture
# ---------------------------------------------------------------------------


def _seed_workspace_from_request(
    workspace: Path, context: A2ARequestContext
) -> list[dict[str, Any]]:
    """Write inbound source files into the scratch workspace.

    Sources, in priority order:

    * ``message.parts`` of kind ``file`` (A2A ``FilePart`` — bytes only;
      ``uri`` references are not resolved).
    * ``metadata.source_files`` (list of ``{"path", "content"}`` dicts).

    Returns a list of seeded file descriptors ``[{"path", "size_bytes"}]``.
    Raises ``ValueError`` if a path escapes the workspace.
    """
    seeded: dict[str, dict[str, Any]] = {}

    if context.message:
        for part in context.message.parts:
            root = part.root
            if not _is_file_part(root):
                continue
            file = getattr(root, "file", None)
            name = getattr(file, "name", None)
            if not name:
                continue
            data_bytes = _decode_file_part_bytes(file)
            if data_bytes is None:
                continue
            target = _safe_workspace_path(workspace, str(name))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data_bytes)
            seeded[str(name)] = {
                "path": str(name),
                "size_bytes": len(data_bytes),
            }

    metadata = context.metadata or {}
    raw_files = metadata.get("source_files")
    if isinstance(raw_files, list):
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            content = entry.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                continue
            target = _safe_workspace_path(workspace, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            seeded[path] = {
                "path": path,
                "size_bytes": len(content.encode("utf-8")),
            }

    return list(seeded.values())


def _safe_workspace_path(workspace: Path, raw: str) -> Path:
    candidate = (workspace / raw).resolve()
    workspace_resolved = workspace.resolve()
    if not _is_relative_to(candidate, workspace_resolved):
        raise ValueError(
            f"Source file path {raw!r} escapes the workspace; "
            "use workspace-relative paths only."
        )
    return candidate


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _decode_file_part_bytes(file: Any) -> bytes | None:
    raw = getattr(file, "bytes", None)
    if isinstance(raw, str) and raw:
        try:
            return base64.b64decode(raw, validate=False)
        except (ValueError, TypeError):
            return None
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    return None


def _snapshot_workspace(workspace: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        snapshot[rel] = _file_sha256(path)
    return snapshot


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_workspace_changes(
    workspace: Path, before: dict[str, str], after: dict[str, str]
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for rel, digest in after.items():
        previous = before.get(rel)
        if previous == digest:
            continue
        action = "modified" if previous else "created"
        changes.append(_describe_file(workspace, rel, action, digest))
    for rel in before:
        if rel in after:
            continue
        changes.append(
            {
                "path": rel,
                "action": "deleted",
                "size_bytes": 0,
                "sha256": before[rel],
            }
        )
    changes.sort(key=lambda entry: entry["path"])
    return changes


def _describe_file(
    workspace: Path, rel: str, action: str, digest: str
) -> dict[str, Any]:
    abs_path = workspace / rel
    size = abs_path.stat().st_size
    entry: dict[str, Any] = {
        "path": rel,
        "action": action,
        "size_bytes": size,
        "sha256": digest,
    }
    if size > _INLINE_FILE_BYTE_LIMIT:
        entry["content_truncated"] = True
        return entry
    raw = abs_path.read_bytes()
    try:
        entry["content"] = raw.decode("utf-8")
        entry["content_truncated"] = False
    except UnicodeDecodeError:
        entry["binary"] = True
        entry["content_base64"] = base64.b64encode(raw).decode("ascii")
        entry["content_truncated"] = False
    return entry


# ---------------------------------------------------------------------------
# Task prompt
# ---------------------------------------------------------------------------


_PER_SKILL_INSTRUCTIONS: dict[str, str] = {
    SKILL_REPOSITORY_INSPECTION: (
        "Inspect the seeded source files in this scratch workspace and propose "
        "what coding work should happen next. Treat the workspace as read-only "
        "for inspection purposes — you may run shell tools to probe the files "
        "but should not produce new code unless the user asked for it. The "
        "final JSON payload must use keys "
        '"repo_summary" (str), "detected_issues" (list[str]), '
        '"recommended_next_task" (str), "risk_level" ("low"|"medium"|"high").'
    ),
    SKILL_PR_REVIEW: (
        "Treat the seeded source files as a snapshot of a branch under review. "
        "Inspect them and report findings without producing new files. The "
        'final JSON payload must use keys "review_findings" (list of {severity, '
        'summary} objects) and "risk_level" ("low"|"medium"|"high").'
    ),
    SKILL_DOCUMENTATION: (
        "Draft documentation for the seeded module(s). Write any documentation "
        "files (e.g. README.md, docs/*.md) DIRECTLY into this workspace; the "
        "workspace contents will be returned to the caller. The final JSON "
        'payload must use keys "documentation_draft" (str summary), '
        '"target_files" (list[str] of paths you wrote), '
        '"risk_level" ("low"|"medium"|"high").'
    ),
    SKILL_BUG_FIX: (
        "Investigate the seeded source files and apply a minimal fix. Edit "
        "the workspace files in place — modified and created files will be "
        "returned to the caller as part of the response. Run any focused "
        "tests you need before submitting. The final JSON payload must use "
        'keys "proposed_action" (object with summary, steps, '
        'modifies_repository), "risk_level" ("low"|"medium"|"high"), '
        '"confirmation_required" (bool).'
    ),
    SKILL_TEST_GENERATION: (
        "Identify behaviour in the seeded source files that lacks coverage and "
        "write proposed test files into this workspace; created files will be "
        "returned to the caller. The final JSON payload must use keys "
        '"proposed_action" (object with summary, steps, modifies_repository), '
        '"risk_level" ("low"|"medium"|"high"), "confirmation_required" (bool).'
    ),
}

_FALLBACK_INSTRUCTION = (
    "Provide a short structured summary of relevant workspace observations. "
    'The final JSON payload must use keys "summary" (str) and "risk_level". '
    "Any source files you generate should be written into this workspace."
)

_SUBMISSION_FORMAT_HINT = (
    "When you are finished, your FINAL bash command must look like this so the "
    "submission contains only your JSON report:\n\n"
    "```mswea_bash_command\n"
    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat <<'EOF'\n"
    "{ ...your json... }\n"
    "EOF\n"
    "```\n"
)


def _task_for_skill(
    skill: str,
    prompt: str,
    seeded: list[dict[str, Any]],
    workspace: Path,
) -> str:
    instruction = _PER_SKILL_INSTRUCTIONS.get(skill, _FALLBACK_INSTRUCTION)
    user_block = prompt.strip() or "(no additional user prompt)"
    seeded_listing = (
        "\n".join(
            f"  - {entry['path']} ({entry['size_bytes']} bytes)" for entry in seeded
        )
        if seeded
        else "  (none — the workspace is empty; generate any code as files in the current directory.)"
    )
    return (
        f"You are running inside a scratch workspace at {workspace}. "
        f"Bash commands execute with this directory as cwd. The host filesystem "
        f"outside this directory is OFF LIMITS — do not cd out of it, do not "
        f"touch /etc, do not push to git, do not call external services.\n\n"
        f"Workspace contents (seeded by the caller):\n{seeded_listing}\n\n"
        f"Any file you create or modify in this workspace will be packaged "
        f"into the A2A response and shipped back to the caller. Treat that as "
        f"your delivery mechanism — do NOT paste source code into the JSON "
        f"submission, write it to disk instead.\n\n"
        f"Skill: {skill}\n"
        f"User request:\n{user_block}\n\n"
        f"Skill-specific instructions:\n{instruction}\n\n"
        f"{_SUBMISSION_FORMAT_HINT}"
    )


# ---------------------------------------------------------------------------
# Structured output construction
# ---------------------------------------------------------------------------


def _structured_output(
    skill: str,
    prompt: str,
    context: A2ARequestContext,
    workspace: Path,
    seeded: list[dict[str, Any]],
    run_result: dict[str, Any],
    generated_files: list[dict[str, Any]],
    delivery_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed = _parse_submission_json(run_result.get("submission", ""))
    base: dict[str, Any] = {
        "skill": skill,
        "context_id": context.context_id,
        "task_id": context.task_id,
        "user_prompt": prompt,
        "workspace": str(workspace),
        "seeded_files": [entry["path"] for entry in seeded],
        "generated_files": generated_files,
        "workspace_summary": _workspace_summary(seeded, generated_files),
        "delivery_result": delivery_result,
        "mini_swe_agent": {
            "n_calls": run_result.get("n_calls"),
            "cost": run_result.get("cost"),
            "exit_status": run_result.get("exit_status"),
        },
    }

    if skill == SKILL_REPOSITORY_INSPECTION:
        base.update(
            {
                "repo_summary": _coerce_str(
                    parsed.get("repo_summary"),
                    default=run_result.get("submission", "").strip()
                    or "No summary provided.",
                ),
                "detected_issues": _coerce_str_list(parsed.get("detected_issues")),
                "recommended_next_task": _coerce_str(
                    parsed.get("recommended_next_task"), default=""
                ),
                "risk_level": _coerce_risk_level(parsed.get("risk_level")),
            }
        )
    elif skill == SKILL_PR_REVIEW:
        base.update(
            {
                "review_findings": _coerce_findings(parsed.get("review_findings")),
                "risk_level": _coerce_risk_level(parsed.get("risk_level")),
            }
        )
    elif skill == SKILL_DOCUMENTATION:
        target_files = _coerce_str_list(parsed.get("target_files"))
        if not target_files:
            target_files = [
                entry["path"]
                for entry in generated_files
                if entry.get("action") in {"created", "modified"}
            ]
        base.update(
            {
                "documentation_draft": _coerce_str(
                    parsed.get("documentation_draft"),
                    default=run_result.get("submission", "").strip(),
                ),
                "target_files": target_files,
                "risk_level": _coerce_risk_level(parsed.get("risk_level")),
            }
        )
    elif skill in WRITE_GATED_SKILLS:
        proposed = (
            parsed.get("proposed_action")
            if isinstance(parsed.get("proposed_action"), dict)
            else {}
        )
        base.update(
            {
                "confirmation_required": bool(
                    parsed.get("confirmation_required", True)
                ),
                "proposed_action": {
                    "skill": skill,
                    "summary": _coerce_str(
                        proposed.get("summary"),
                        default=f"Proposed {skill} plan.",
                    ),
                    "steps": _coerce_str_list(proposed.get("steps")),
                    "modifies_repository": bool(
                        proposed.get("modifies_repository", True)
                    ),
                    "requested_goal": prompt,
                },
                "risk_level": _coerce_risk_level(
                    parsed.get("risk_level"), default="medium"
                ),
            }
        )
    else:
        base.update(
            {
                "summary": _coerce_str(
                    parsed.get("summary"),
                    default=run_result.get("submission", "").strip(),
                ),
                "risk_level": _coerce_risk_level(parsed.get("risk_level")),
            }
        )

    return base


def _workspace_summary(
    seeded: list[dict[str, Any]], generated: list[dict[str, Any]]
) -> dict[str, Any]:
    counts = {"created": 0, "modified": 0, "deleted": 0}
    for entry in generated:
        action = entry.get("action")
        if action in counts:
            counts[action] += 1
    return {
        "seeded_count": len(seeded),
        "generated_count": len(generated),
        **counts,
    }


def _text_summary(
    skill: str,
    output: dict[str, Any],
    run_result: dict[str, Any],
    generated_files: list[dict[str, Any]],
) -> str:
    file_suffix = ""
    if generated_files:
        names = ", ".join(entry["path"] for entry in generated_files[:3])
        more = "" if len(generated_files) <= 3 else f" (+{len(generated_files) - 3} more)"
        file_suffix = f" Returned files: {names}{more}."
    if skill == SKILL_REPOSITORY_INSPECTION:
        body = (
            output.get("recommended_next_task")
            or output.get("repo_summary")
            or "Repository inspection complete."
        )
    elif skill == SKILL_PR_REVIEW:
        findings = output.get("review_findings") or []
        body = (
            f"PR review complete: {len(findings)} finding(s)."
            if findings
            else "PR review complete."
        )
    elif skill == SKILL_DOCUMENTATION:
        body = output.get("documentation_draft") or "Documentation draft prepared."
    elif skill in WRITE_GATED_SKILLS:
        body = (
            output.get("proposed_action", {}).get("summary")
            or f"{skill} plan prepared."
        )
    else:
        body = (
            output.get("summary")
            or run_result.get("submission")
            or f"{skill} complete."
        )
    return f"{body}{file_suffix}"


# ---------------------------------------------------------------------------
# Submission JSON parsing & coercion helpers
# ---------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_submission_json(submission: str) -> dict[str, Any]:
    if not submission:
        return {}
    text = submission.strip()
    candidates: list[str] = []
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1))
    candidates.append(text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _coerce_str(value: Any, *, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _coerce_findings(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    findings: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            findings.append(
                {
                    "severity": _coerce_str(item.get("severity"), default="info"),
                    "summary": _coerce_str(item.get("summary"), default=""),
                }
            )
        elif isinstance(item, str):
            findings.append({"severity": "info", "summary": item})
    return findings


_RISK_VALUES = {"low", "medium", "high"}


def _coerce_risk_level(value: Any, *, default: str = "low") -> str:
    if isinstance(value, str) and value.lower() in _RISK_VALUES:
        return value.lower()
    return default


async def _emit_failure(
    updater: A2ATaskUpdater,
    skill: str,
    context: A2ARequestContext,
    workspace: Path,
    error: BaseException,
) -> None:
    payload = {
        "skill": skill,
        "context_id": context.context_id,
        "task_id": context.task_id,
        "workspace": str(workspace),
        "generated_files": [],
        "error": f"{type(error).__name__}: {error}",
        "risk_level": "low",
    }
    artifact_id, artifact_name = _ARTIFACT_IDS.get(
        skill,
        (f"coding-{skill.replace('/', '-')}", f"coding_{skill.replace('/', '_')}"),
    )
    await updater.add_artifact(
        [_data_part(payload)],
        artifact_id=artifact_id,
        name=artifact_name,
        metadata={"skill": skill, "content_type": "application/json", "error": True},
    )
    await updater.failed(
        message=updater.new_agent_message(
            [_text_part(payload["error"])],
            metadata={"structured_output": payload},
        )
    )


# ---------------------------------------------------------------------------
# mini-swe-agent runner
# ---------------------------------------------------------------------------


def _default_minisweagent_runner(
    *,
    task: str,
    skill: str,
    workspace: str,
    limits: dict[str, Any],
) -> dict[str, Any]:
    """Run mini-swe-agent's ``DefaultAgent`` against ``workspace``.

    Requires ``CODING_AGENT_MODEL`` (or ``MSWEA_MODEL_NAME``) to be set to a
    litellm-compatible model id.

    YOLO note: ``DefaultAgent`` has no confirmation step. Its sibling
    ``InteractiveAgent`` does — using ``DefaultAgent`` is the explicit choice
    that makes this server callable autonomously by other agents. The
    ``LocalEnvironment`` ``cwd`` is bound to the per-request scratch
    workspace, so the LM's bash cannot reach files outside it.
    """
    import yaml
    from minisweagent import package_dir as _mswea_package_dir
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.agents.interactive import InteractiveAgent
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.exceptions import Submitted
    from minisweagent.models.litellm_model import LitellmModel

    _SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

    class _LenientSubmissionEnvironment(LocalEnvironment):
        """LocalEnvironment that also accepts the submit marker mid-output.

        Upstream's ``_check_finished`` only fires when the marker is the very
        first stripped line of stdout. Some models chain the marker after a
        successful command (``... && echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT``)
        and the upstream check misses it, so the inner loop burns its full
        ``step_limit`` retrying instead of exiting. This subclass first defers
        to the strict upstream check (zero behaviour change for well-behaved
        models) and then does a permissive scan: if any line equals the marker
        and the command succeeded, treat it as a submission.
        """

        def _check_finished(self, output: dict) -> None:
            super()._check_finished(output)
            if output.get("returncode") != 0:
                return
            text = output.get("output", "") or ""
            lines = text.splitlines()
            for idx, line in enumerate(lines):
                if line.strip() == _SUBMIT_MARKER:
                    submission = "\n".join(lines[idx + 1 :])
                    raise Submitted(
                        {
                            "role": "exit",
                            "content": submission,
                            "extra": {
                                "exit_status": "Submitted",
                                "submission": submission,
                            },
                        }
                    )

    model_name = os.getenv("CODING_AGENT_MODEL") or os.getenv("MSWEA_MODEL_NAME")
    if not model_name:
        raise RuntimeError(
            "CodingAgentExecutor requires CODING_AGENT_MODEL or MSWEA_MODEL_NAME "
            "to point at a litellm-compatible model. Tests should pass an "
            "explicit agent_runner instead."
        )

    config = yaml.safe_load(
        Path(_mswea_package_dir, "config", "default.yaml").read_text()
    )
    agent_kwargs = (config.get("agent") or {}) | {
        "step_limit": int(limits.get("step_limit", 12)),
        "cost_limit": float(limits.get("cost_limit", 0.50)),
    }
    model = LitellmModel(
        model_name=model_name,
        model_kwargs=(config.get("model") or {}).get("model_kwargs") or {},
    )
    env = _LenientSubmissionEnvironment(
        cwd=workspace,
        env=(config.get("environment") or {}).get("env") or {},
    )
    agent = DefaultAgent(model, env, **agent_kwargs)
    if isinstance(agent, InteractiveAgent):
        raise RuntimeError(
            "CodingAgentExecutor requires DefaultAgent (yolo). Refusing to run "
            "an interactive agent over the A2A boundary."
        )
    extra = agent.run(task)
    return {
        "submission": extra.get("submission", ""),
        "exit_status": extra.get("exit_status", "Submitted"),
        "n_calls": agent.n_calls,
        "cost": agent.cost,
        "messages": [
            {"role": m.get("role"), "content": _truncate(m.get("content"), 2000)}
            for m in agent.messages[-6:]
        ],
    }


def _truncate(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


# ---------------------------------------------------------------------------
# Delivery: discover an email-capable agent and ship the generated files
# ---------------------------------------------------------------------------


_DELIVERY_KIND_EMAIL = "email"
_DEFAULT_DELIVERY_SKILL_QUERY = "email/send"


def _delivery_from_context(context: A2ARequestContext) -> dict[str, Any] | None:
    """Extract a ``delivery`` block from request metadata, if present."""
    metadata = context.metadata or {}
    delivery = metadata.get("delivery")
    if isinstance(delivery, dict) and delivery:
        return delivery
    return None


async def _attempt_delivery(
    *,
    delivery: dict[str, Any],
    skill: str,
    user_prompt: str,
    generated_files: list[dict[str, Any]],
    context_id: str,
    directory: Any | None,
    transport: Any | None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Discover a target agent through the directory and ship files to it.

    Returns a ``delivery_result`` dict that gets appended to the coding
    agent's structured_output.
    """
    kind = str(delivery.get("kind", _DELIVERY_KIND_EMAIL)).lower()
    if kind != _DELIVERY_KIND_EMAIL:
        return {
            "status": "unsupported_kind",
            "kind": kind,
            "supported_kinds": [_DELIVERY_KIND_EMAIL],
        }
    if not generated_files:
        return {"status": "no_files", "kind": kind}

    to_list = _normalize_recipients(delivery.get("to"))
    if not to_list:
        return {"status": "missing_recipient", "kind": kind}

    skill_query = str(delivery.get("skill_query", _DEFAULT_DELIVERY_SKILL_QUERY))

    try:
        client = directory or _build_directory_client(delivery)
    except Exception as exc:
        return {
            "status": "directory_unavailable",
            "kind": kind,
            "skill_query": skill_query,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        matches = await asyncio.to_thread(client.search_agents, skill=skill_query)
    except Exception as exc:
        return {
            "status": "discovery_failed",
            "kind": kind,
            "skill_query": skill_query,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not matches:
        return {
            "status": "no_agent_found",
            "kind": kind,
            "skill_query": skill_query,
        }
    discovered = matches[0]

    subject = str(
        delivery.get("subject")
        or f"[Coding Agent] Generated files from {skill}"
    )
    intro = str(delivery.get("body") or _default_email_intro(skill, user_prompt))
    body_text = _format_email_body(intro, generated_files)

    try:
        result = await _send_a2a_email_request(
            agent=discovered,
            to=to_list,
            cc=_normalize_recipients(delivery.get("cc")),
            bcc=_normalize_recipients(delivery.get("bcc")),
            subject=subject,
            body=body_text,
            context_id=context_id,
            transport=transport,
            confirmed=confirmed,
        )
    except Exception as exc:
        return {
            "status": "send_failed",
            "kind": kind,
            "agent_id": getattr(discovered, "agent_id", None),
            "agent_endpoint": getattr(discovered, "endpoint", None),
            "skill_query": skill_query,
            "error": f"{type(exc).__name__}: {exc}",
        }

    state = result.get("state")
    if state == "input_required" and result.get("error") is None:
        status = "pending_confirmation"
    elif state == "completed" and result.get("error") is None:
        status = "sent"
    else:
        status = "send_rejected"
    return {
        "status": status,
        "kind": kind,
        "agent_id": getattr(discovered, "agent_id", None),
        "agent_endpoint": getattr(discovered, "endpoint", None),
        "skill_query": skill_query,
        "to": to_list,
        "subject": subject,
        "file_count": len(generated_files),
        "send_state": state,
        "send_message_id": result.get("message_id"),
        "send_summary": result.get("summary"),
    }


class _DirctlError(RuntimeError):
    """Raised when a `dirctl` invocation exits non-zero or returns unparseable output."""


class _DirctlDirectory:
    """Talk to a real ADS via the ``dirctl`` CLI.

    The Python ``agntcy_dir`` SDK is currently broken in this checkout (its
    generated protobuf needs a runtime newer than the pinned ``protobuf``).
    Shelling out to ``dirctl`` works around that — it is the same gRPC client
    operators use directly, packaged as a CLI.

    The class implements the slice of the directory API the coding agent needs:

    * ``search_agents(skill)`` — list local records, pull each, and filter by
      ``annotations.agentic_network.custom_skill_tags`` plus
      ``modules[].data.card_data.skills[].id``. Only A2A records (those that
      carry the ``/modules/integration/a2a`` label) are considered.
    * ``push(record_path) -> cid`` — ``dirctl push <file> --output raw``.
    * ``publish(cid)`` / ``unpublish(cid)`` — ``dirctl routing publish|unpublish``.
    """

    def __init__(
        self,
        server_addr: str,
        dirctl_path: str = "dirctl",
        timeout: float = 30.0,
        extra_args: Sequence[str] = (),
    ) -> None:
        self.server_addr = server_addr
        self.dirctl_path = dirctl_path
        self.timeout = timeout
        self._extra_args = list(extra_args)

    def _base_cmd(self) -> list[str]:
        return [self.dirctl_path, "--server-addr", self.server_addr, *self._extra_args]

    def _run(self, args: Sequence[str]) -> str:
        cmd = self._base_cmd() + list(args)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise _DirctlError(
                f"`{self.dirctl_path}` not found on PATH. Install dirctl or "
                "pass --dirctl-path."
            ) from exc
        if result.returncode != 0:
            raise _DirctlError(
                f"`{' '.join(cmd)}` exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout

    def list_records(self) -> list[dict[str, Any]]:
        out = self._run(["routing", "list", "--output", "json"])
        if not out.strip():
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise _DirctlError(f"Failed to parse `routing list` JSON: {exc}") from exc
        return data if isinstance(data, list) else []

    def pull_record(self, cid: str) -> dict[str, Any]:
        out = self._run(["pull", cid, "--output", "json"])
        if not out.strip():
            raise _DirctlError(f"`pull {cid}` returned no output.")
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise _DirctlError(f"Failed to parse `pull` JSON for {cid}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def search_agents(self, skill: str) -> list[Any]:
        candidates = self.list_records()
        results: list[Any] = []
        for entry in candidates:
            cid = (entry.get("record_ref") or {}).get("cid")
            labels = entry.get("labels") or []
            if not cid or "/modules/integration/a2a" not in labels:
                continue
            try:
                record = self.pull_record(cid)
            except _DirctlError:
                continue
            if not _record_supports_custom_skill(record, skill):
                continue
            agent = _record_to_discovered_agent(record, cid)
            if agent is not None:
                results.append(agent)
        return results

    def push(self, record_path: Path) -> str:
        out = self._run(["push", str(record_path), "--output", "raw"])
        cid = out.strip()
        if not cid:
            raise _DirctlError("`dirctl push` produced no CID on stdout.")
        return cid

    def publish(self, cid: str) -> None:
        self._run(["routing", "publish", cid])

    def unpublish(self, cid: str) -> None:
        self._run(["routing", "unpublish", cid])


def _record_supports_custom_skill(record: dict[str, Any], skill: str) -> bool:
    """Return True if the ADS record advertises ``skill`` as a custom skill tag.

    Looks at the AgenticNetwork annotation we set during export, plus the
    raw card_data carried in modules[].data so non-AgenticNetwork agents
    (which omit our annotation) can still match by their A2A skill ids.
    """
    annotations = record.get("annotations") or {}
    tags = annotations.get("agentic_network.custom_skill_tags") or ""
    if any(tag.strip() == skill for tag in tags.split(",")):
        return True
    for module in record.get("modules") or []:
        data = (module or {}).get("data") or {}
        card_data = data.get("card_data") or {}
        for entry in card_data.get("skills") or []:
            if (entry or {}).get("id") == skill:
                return True
    return False


def _record_to_discovered_agent(record: dict[str, Any], cid: str) -> Any:
    """Translate a dirctl-pulled record into an internal DiscoveredAgent."""
    from v2_agent_network.services.common.models import DiscoveredAgent

    annotations = record.get("annotations") or {}
    a2a_url = annotations.get("a2a.url")
    if not a2a_url:
        for locator in record.get("locators") or []:
            la = (locator or {}).get("annotations") or {}
            if la.get("a2a.url"):
                a2a_url = la["a2a.url"]
                break
    if not a2a_url:
        for module in record.get("modules") or []:
            data = (module or {}).get("data") or {}
            card_data = data.get("card_data") or {}
            if card_data.get("url"):
                a2a_url = card_data["url"]
                break
    if not a2a_url:
        return None
    agent_id = (
        annotations.get("agentic_network.agent_id")
        or str(record.get("name") or "unknown").rstrip("/").split("/")[-1]
    )
    name = annotations.get("agentic_network.display_name") or str(
        record.get("name") or agent_id
    )
    organization = annotations.get("agentic_network.organization") or "unknown"
    description = str(record.get("description") or "")
    return DiscoveredAgent(
        agent_id=agent_id,
        name=name,
        organization=organization,
        description=description,
        endpoint=a2a_url,
        transport="http",
        source_record_id=cid,
        source_card_url=a2a_url,
    )


def _build_directory_client(delivery: dict[str, Any]) -> Any:
    """Pick a directory backend.

    Order:
    1. ``delivery.fake_ads`` or ``AGENT_NETWORK_FAKE_ADS`` → in-memory fake
       (only useful for tests / single-process demos).
    2. ``delivery.directory_url`` / ``CODING_AGENT_DIRECTORY_URL`` / ``ADS_URL``
       → ``_DirctlDirectory`` shelling out to the ``dirctl`` CLI.
    3. Otherwise, raise — we have no directory to talk to.
    """
    fake_ads_value = delivery.get("fake_ads")
    if fake_ads_value is None:
        fake_ads_value = os.getenv(
            "AGENT_NETWORK_FAKE_ADS", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
    if fake_ads_value:
        from v2_agent_network.services.directory.client import DirectoryClient
        return DirectoryClient(fake_ads=True)

    server_addr = (
        delivery.get("directory_url")
        or os.getenv("CODING_AGENT_DIRECTORY_URL")
        or os.getenv("ADS_URL")
    )
    if server_addr:
        return _DirctlDirectory(server_addr=server_addr)

    raise RuntimeError(
        "No directory configured. Set delivery.directory_url, "
        "CODING_AGENT_DIRECTORY_URL, or ADS_URL — or pass fake_ads=true."
    )


async def _send_a2a_email_request(
    *,
    agent: Any,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body: str,
    context_id: str,
    transport: Any | None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Send an A2A ``email/send`` request to the discovered agent.

    When ``confirmed`` is False, the email agent's standard policy gate
    runs and may return ``input_required`` asking the user to approve.
    Pass ``confirmed=True`` only after the user has actually approved
    sending this exact message.
    """
    from v2_agent_network.services.a2a.client import A2AClient
    from v2_agent_network.services.a2a.transport import HTTPA2ATransport
    from v2_agent_network.services.common.models import (
        A2AMessage,
        A2APart,
        A2ATaskRequest,
    )

    payload: dict[str, Any] = {
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": subject,
        "body": body,
    }
    metadata: dict[str, Any] = {"skill": _DEFAULT_DELIVERY_SKILL_QUERY, **payload}
    if confirmed:
        metadata["confirmation_status"] = "confirmed"
        metadata["confirmed"] = True

    request = A2ATaskRequest(
        task_id=str(uuid.uuid4()),
        context_id=context_id,
        target_agent_id=agent.agent_id,
        skill=_DEFAULT_DELIVERY_SKILL_QUERY,
        user_goal=f"Email generated source files to {', '.join(to)}",
        messages=[
            A2AMessage(
                role="user",
                parts=[
                    A2APart(kind="text", text=body),
                    A2APart(
                        kind="data",
                        content_type="application/json",
                        data=payload,
                    ),
                ],
            )
        ],
        metadata=metadata,
    )

    if transport is None:
        transport = HTTPA2ATransport()
    client = A2AClient(transport)
    result = await client.send_task(agent, request)
    state = result.state.value if hasattr(result.state, "value") else str(result.state)
    structured = result.structured_output or {}
    summary = None
    if result.messages:
        last = result.messages[-1]
        for part in last.parts:
            if part.kind == "text" and part.text:
                summary = part.text
                break
    return {
        "state": state,
        "message_id": structured.get("message_id"),
        "error": structured.get("error"),
        "summary": summary,
    }


def _normalize_recipients(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _default_email_intro(skill: str, user_prompt: str) -> str:
    first_line = (user_prompt.strip().splitlines() or ["(no prompt)"])[0]
    return (
        f"The coding agent ran the {skill} skill in response to:\n"
        f"  {first_line}\n\n"
        "Generated files are inlined below. (The email agent's A2A boundary "
        "does not currently accept attachments, so the source code is in the "
        "body rather than as separate files.)"
    )


def _format_email_body(intro: str, generated_files: list[dict[str, Any]]) -> str:
    lines: list[str] = [intro.rstrip(), ""]
    lines.append(f"Generated {len(generated_files)} file(s):")
    lines.append("")
    for entry in generated_files:
        path = entry["path"]
        action = entry.get("action", "created")
        lines.append(f"=== {path} ({action}, {entry.get('size_bytes', 0)} bytes) ===")
        if entry.get("content_truncated"):
            lines.append(f"[content truncated; sha256={entry.get('sha256')}]")
        elif entry.get("binary"):
            preview = entry.get("content_base64", "")
            lines.append(f"[binary file, base64-encoded preview]")
            lines.append(preview[:512] + ("…" if len(preview) > 512 else ""))
        elif "content" in entry:
            lines.append(entry["content"])
        else:
            lines.append("[no content available]")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "mini-swe-agent A2A wrapper. Speak A2A on a port the AgenticNetwork "
            "PI agent can reach; run mini-swe-agent's DefaultAgent in YOLO mode "
            "for the registered code/* skills. Each request runs in a fresh "
            "scratch workspace and the workspace contents are returned to the "
            "caller in the A2A response."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    a2a_parser = subparsers.add_parser(
        "serve-a2a",
        help="Run the coding agent as an A2A HTTP server.",
    )
    a2a_parser.add_argument(
        "--host",
        default=os.getenv("CODING_AGENT_A2A_HOST", "0.0.0.0"),
        help="Host interface to bind. Default: 0.0.0.0.",
    )
    a2a_parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("CODING_AGENT_A2A_PORT", "9106")),
        help="Port for the A2A HTTP server. Default: 9106 (matches the card).",
    )
    a2a_parser.add_argument(
        "--card",
        type=Path,
        default=DEFAULT_CODING_A2A_CARD,
        help="A2A Agent Card JSON file.",
    )
    a2a_parser.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help=(
            "Parent directory under which per-request scratch workspaces are "
            "created. Defaults to CODING_AGENT_WORKSPACE_ROOT or the OS tempdir."
        ),
    )
    a2a_parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Do not delete scratch workspaces after the response is sent.",
    )
    a2a_parser.add_argument(
        "--allow-missing-model",
        action="store_true",
        help=(
            "Skip the upfront CODING_AGENT_MODEL check. Useful when running "
            "tests against an injected fake runner. Default: off."
        ),
    )
    a2a_parser.add_argument(
        "--directory-server",
        default=os.getenv("CODING_AGENT_DIRECTORY_URL") or os.getenv("ADS_URL"),
        help=(
            "ADS server address for `dirctl` (e.g. 10.229.117.37:8888). When "
            "set, the coding agent self-publishes on startup and unpublishes "
            "on exit, and outbound `delivery` requests use this directory for "
            "discovery. Defaults to CODING_AGENT_DIRECTORY_URL or ADS_URL."
        ),
    )
    a2a_parser.add_argument(
        "--dirctl-path",
        default="dirctl",
        help="Path to the dirctl binary. Default: search PATH.",
    )
    a2a_parser.add_argument(
        "--no-publish",
        action="store_true",
        help=(
            "Do not push/publish this agent's record to ADS even if "
            "--directory-server is set. Discovery still uses dirctl."
        ),
    )
    a2a_parser.add_argument(
        "--log-level",
        default=os.getenv("CODING_AGENT_LOG_LEVEL", "info"),
        choices=["debug", "info", "warning", "error", "critical"],
        help=(
            "Log level for the coding agent and uvicorn. Default: info "
            "(env: CODING_AGENT_LOG_LEVEL). Use debug to see seeded file paths "
            "and per-step trace output."
        ),
    )

    run_parser = subparsers.add_parser(
        "run-skill",
        help="Run a single coding skill from the CLI (no A2A round-trip).",
    )
    run_parser.add_argument(
        "skill",
        help="Skill id, e.g. code/repository_inspection.",
    )
    run_parser.add_argument(
        "--prompt",
        default="",
        help="User prompt forwarded to the skill.",
    )
    run_parser.add_argument(
        "--input-file",
        action="append",
        default=[],
        metavar="HOST_PATH:WORKSPACE_PATH",
        help=(
            "Seed the scratch workspace with a file. Repeatable. Format: "
            "'host_path:workspace_path' — e.g. 'src/foo.py:src/foo.py'."
        ),
    )
    run_parser.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help="Parent directory for the scratch workspace.",
    )
    run_parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Do not delete the scratch workspace after the run.",
    )

    subparsers.add_parser(
        "tools",
        help="Print every skill the executor handles.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "tools":
        for skill, (artifact_id, _) in _ARTIFACT_IDS.items():
            gated = "write (PI-gated)" if skill in WRITE_GATED_SKILLS else "read"
            print(f"{skill}: artifact={artifact_id} action={gated}")
        return 0

    if args.command == "run-skill":
        return _cli_run_skill(args)

    if args.command == "serve-a2a":
        log_level = args.log_level.upper()
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        # Quiet down a2a-sdk's internal request handler at INFO; raise it for DEBUG.
        logging.getLogger("a2a").setLevel(log_level)

        if not args.allow_missing_model:
            _check_model_configured()

        _require_a2a()
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError(
                "The uvicorn package is required for serve-a2a."
            ) from exc

        workspace_root = (
            str(args.workspace_root.resolve()) if args.workspace_root else None
        )
        card = load_coding_a2a_agent_card(args.card)

        delivery_directory = None
        if args.directory_server:
            delivery_directory = _DirctlDirectory(
                server_addr=args.directory_server,
                dirctl_path=args.dirctl_path,
            )

        executor = CodingAgentExecutor(
            workspace_root=workspace_root,
            cleanup=not args.keep_workspace,
            delivery_directory=delivery_directory,
        )
        app = create_coding_a2a_app(card, executor=executor)

        published_cid: str | None = None
        if args.directory_server and not args.no_publish:
            try:
                published_cid = _publish_self_to_ads(
                    args.card,
                    args.directory_server,
                    dirctl_path=args.dirctl_path,
                )
                logger.info(
                    "published to ADS: server=%s cid=%s",
                    args.directory_server, published_cid,
                )
            except _DirctlError as exc:
                logger.error("failed to publish to ADS: %s", exc)
                return 3

        logger.info(
            "serving A2A: bind=http://%s:%s/ card_url=%s workspace_root=%s",
            args.host, args.port, card.url, executor._workspace_root,
        )
        logger.warning(
            "YOLO mode: bash commands run without confirmation. Run inside a sandbox."
        )
        try:
            uvicorn.run(
                app,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
            )
        finally:
            if published_cid is not None:
                try:
                    _unpublish_self_from_ads(
                        args.directory_server,
                        published_cid,
                        dirctl_path=args.dirctl_path,
                    )
                    logger.info(
                        "unpublished from ADS: server=%s cid=%s",
                        args.directory_server, published_cid,
                    )
                except _DirctlError as exc:
                    logger.error(
                        "failed to unpublish cid=%s: %s", published_cid, exc,
                    )
        return 0

    return 2


def _publish_self_to_ads(
    card_path: Path, server_addr: str, *, dirctl_path: str = "dirctl"
) -> str:
    """Convert this agent's card to an ADS record, push it, and announce it.

    Returns the CID assigned by the directory. Caller is responsible for
    calling :func:`_unpublish_self_from_ads` on shutdown to remove the
    routing announcement.
    """
    from _ads_record import agent_card_to_ads_record, load_agent_card

    card = load_agent_card(card_path)
    record = agent_card_to_ads_record(card)
    fh = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".record.json", delete=False
    )
    record_path = Path(fh.name)
    try:
        json.dump(record, fh, indent=2)
        fh.close()
        directory = _DirctlDirectory(server_addr=server_addr, dirctl_path=dirctl_path)
        cid = directory.push(record_path)
        directory.publish(cid)
        return cid
    finally:
        try:
            record_path.unlink()
        except FileNotFoundError:
            pass


def _unpublish_self_from_ads(
    server_addr: str, cid: str, *, dirctl_path: str = "dirctl"
) -> None:
    directory = _DirctlDirectory(server_addr=server_addr, dirctl_path=dirctl_path)
    directory.unpublish(cid)


def _cli_run_skill(args: argparse.Namespace) -> int:
    workspace_root = (
        Path(args.workspace_root).resolve()
        if args.workspace_root
        else Path(tempfile.gettempdir())
    )
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace = workspace_root / f"cli-{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True, exist_ok=False)
    seeded: list[dict[str, Any]] = []
    try:
        for spec in args.input_file or []:
            host_path, _, ws_path = spec.partition(":")
            host_path = host_path.strip()
            ws_path = (ws_path or host_path).strip()
            if not host_path:
                raise ValueError(f"--input-file expects HOST_PATH[:WORKSPACE_PATH], got {spec!r}")
            target = _safe_workspace_path(workspace, ws_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(Path(host_path).read_bytes())
            seeded.append({"path": ws_path, "size_bytes": target.stat().st_size})

        before = _snapshot_workspace(workspace)
        run_result = _default_minisweagent_runner(
            task=_task_for_skill(args.skill, args.prompt, seeded, workspace),
            skill=args.skill,
            workspace=str(workspace),
            limits=_limits_for_skill(args.skill),
        )
        after = _snapshot_workspace(workspace)
        generated_files = _capture_workspace_changes(workspace, before, after)

        report = {
            "skill": args.skill,
            "workspace": str(workspace),
            "seeded_files": [entry["path"] for entry in seeded],
            "generated_files": generated_files,
            "workspace_summary": _workspace_summary(seeded, generated_files),
            "mini_swe_agent": {
                "n_calls": run_result.get("n_calls"),
                "cost": run_result.get("cost"),
                "exit_status": run_result.get("exit_status"),
            },
            "submission": run_result.get("submission", ""),
        }
        sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
        return 0
    finally:
        if not args.keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


def _check_model_configured() -> None:
    if os.getenv("CODING_AGENT_MODEL") or os.getenv("MSWEA_MODEL_NAME"):
        return
    sys.stderr.write(
        "[coding_agent] CODING_AGENT_MODEL (or MSWEA_MODEL_NAME) is not set. "
        "The coding agent will fail on first A2A request because the default "
        "mini-swe-agent runner needs a litellm-compatible model id, e.g.\n"
        "  export CODING_AGENT_MODEL=anthropic/claude-sonnet-4-5-20250929\n"
        "Pass --allow-missing-model to bypass this check.\n"
    )
    raise SystemExit(2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
