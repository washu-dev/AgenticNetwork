"""A2A SDK access — the import guard + thin wrappers around `a2a-sdk` types.

SUPPORT FILE — pure plumbing shared by executor.py and serve.py. You should not
need to edit it.

Why the guard: if `a2a-sdk` is not installed, the executor base class degrades
to `object` so this package still *imports* (handy for unit-testing the tool
bodies or running `serve.py check`), but actually *serving* raises a clear
error via require_a2a().
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # for type checkers only; never executed at runtime
    from a2a.server.events import EventQueue
    from a2a.server.tasks import TaskUpdater

try:
    from a2a.server.agent_execution import AgentExecutor as AgentExecutorBase
    from a2a.server.apps import A2AStarletteApplication
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks import InMemoryTaskStore
    from a2a.server.tasks import TaskUpdater as _TaskUpdater
    from a2a.types import AgentCard
    from a2a.types import DataPart as _DataPart
    from a2a.types import Part as _Part
    from a2a.types import TaskState
    from a2a.types import TextPart as _TextPart

    A2A_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # degrade gracefully
    A2A_IMPORT_ERROR = exc
    AgentExecutorBase = object  # type: ignore[assignment,misc]
    A2AStarletteApplication = None
    DefaultRequestHandler = None
    InMemoryTaskStore = None
    _TaskUpdater = None
    AgentCard = None
    _DataPart = None
    _Part = None
    _TextPart = None
    TaskState = None


def require_a2a() -> None:
    """Raise a clear error if `a2a-sdk` is missing (called before serving)."""
    if A2A_IMPORT_ERROR is not None:
        raise RuntimeError(
            "a2a-sdk is required to serve this agent. Install it into the agent's "
            f"environment (pip install a2a-sdk). Original import error: {A2A_IMPORT_ERROR}"
        )


def data_part(data: dict[str, Any]) -> Any:
    """Wrap a JSON-able dict as an A2A DataPart (the machine-readable channel)."""
    require_a2a()
    return _Part(root=_DataPart(data=data))


def text_part(text: str) -> Any:
    """Wrap text as an A2A TextPart (the human-readable channel)."""
    require_a2a()
    return _Part(root=_TextPart(text=text))


def is_data_part(value: Any) -> bool:
    return _DataPart is not None and isinstance(value, _DataPart)


def task_updater(event_queue: "EventQueue", task_id: str, context_id: str) -> "TaskUpdater":
    """The handle the executor uses to stream status/artifacts/result back out."""
    require_a2a()
    return _TaskUpdater(event_queue, task_id, context_id)
