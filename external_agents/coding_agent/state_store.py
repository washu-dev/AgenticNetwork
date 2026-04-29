"""Filesystem-backed per-task state for the coding agent.

When the agent loop pauses on a user-confirmation prompt (e.g. the email
agent returns ``input_required``), the executor must persist enough state
to resume on the next ``message/send`` request that targets the same
``task_id``: the LLM message history, the in-flight tool call awaiting a
user reply, and pointers to the on-disk workspace.

Each task's state lives in a single JSON file under ``root``. The store is
deliberately simple — JSON, atomic-rename writes, no locking. It assumes
one executor instance per process and cooperative scheduling within
``asyncio``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CodingTaskState:
    state_key: str  # storage key, normally the A2A context_id
    task_id: str  # the current request's A2A task_id (informational only;
                  # changes per request because the installed a2a-sdk
                  # generates a fresh task_id per ``message/send``)
    context_id: str
    user_prompt: str
    workspace: str
    seeded_files: list[dict[str, Any]] = field(default_factory=list)
    initial_snapshot: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_invocations: list[dict[str, Any]] = field(default_factory=list)
    run_coder_calls: list[dict[str, Any]] = field(default_factory=list)
    delivery_results: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] | None = None
    status: str = "running"
    n_steps: int = 0
    n_continuations: int = 0
    started_at: float = 0.0
    last_updated: float = 0.0


class CodingTaskStateStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return self.root / f"{safe}.json"

    def load(self, key: str) -> CodingTaskState | None:
        path = self._path(key)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return CodingTaskState(**data)

    def save(self, state: CodingTaskState) -> None:
        path = self._path(state.state_key)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(asdict(state), indent=2, default=str), encoding="utf-8"
        )
        tmp.replace(path)

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()
