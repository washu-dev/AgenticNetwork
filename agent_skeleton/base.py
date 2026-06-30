from __future__ import annotations
import mimetypes
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


class FileInput:
    """Wraps a binary file arriving from an A2A FilePart."""

    def __init__(self, data: bytes, name: str | None = None, mime_type: str | None = None):
        self.bytes = data
        self.name = name
        self.mime_type = mime_type or _detect_mime(data, name)

    @contextmanager
    def as_tempfile(self, suffix: str | None = None) -> Generator[Path, None, None]:
        """Write bytes to a temp file, yield its Path, clean up on exit."""
        if suffix is None and self.name:
            suffix = Path(self.name).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(self.bytes)
            tmp = Path(f.name)
        try:
            yield tmp
        finally:
            tmp.unlink(missing_ok=True)


def _detect_mime(data: bytes, name: str | None) -> str:
    if name:
        guessed, _ = mimetypes.guess_type(name)
        if guessed:
            return guessed
    # Sniff common signatures
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:2] in (b"\xff\xd8", b"\x89P"):
        return "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"
    return "application/octet-stream"


class AgentHandler(ABC):
    """
    Base class for custom uploaded agents.

    Subclass this, implement handle_structured(), upload the file via the
    registration service UI. The framework handles A2A, ADS, and lifecycle.

    Minimum implementation:

        from agent_skeleton import AgentHandler, FileInput

        class MyHandler(AgentHandler):
            async def handle_structured(
                self, user_input: str, files: list[FileInput] = []
            ) -> dict:
                return {"answer": "your response"}
    """

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    async def handle_structured(
        self,
        user_input: str,
        files: list[FileInput] = [],
    ) -> dict:
        """
        Core logic. Must return a dict containing an "answer" key.

        The "answer" value is the human-readable text response shown to the user.
        All other keys are passed to the planner as structured output.

        Raises ValueError at runtime if "answer" is missing.
        """
        ...

    async def handle(self, user_input: str, files: list[FileInput] = []) -> str:
        """Returns the "answer" from handle_structured. Override if needed."""
        result = await self.handle_structured(user_input, files)
        return result.get("answer", str(result))
