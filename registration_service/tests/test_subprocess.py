"""Integration test: a multi-file custom agent spawned by SubprocessBackend can
import its sibling modules (project root on PYTHONPATH) and serve over A2A.

Skipped when a2a-sdk / uvicorn aren't importable (the engine can be tested
without them, but *serving* requires them). Spawns a real subprocess bound to a
free port, so it's slower than the pure-unit tests.
"""

from __future__ import annotations

import socket
import uuid

import pytest

pytest.importorskip("a2a")
pytest.importorskip("uvicorn")

from registration_service.backend import SubprocessBackend  # noqa: E402
from registration_service.models import CustomConfig  # noqa: E402

pytestmark = pytest.mark.asyncio

# handler.py imports a sibling module; no pyproject.toml → PYTHONPATH-only (no venv).
_ENTRY = (
    "from agent_skeleton import AgentHandler\n"
    "from mylib import tag\n"
    "class MultiHandler(AgentHandler):\n"
    "    async def handle_structured(self, user_input, files=[]):\n"
    "        return {'answer': tag(user_input)}\n"
)
_SIBLING = "def tag(s): return 'tagged:' + s\n"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_multifile_subprocess_resolves_sibling_import(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent_id = str(uuid.uuid4())
    hd = tmp_path / agent_id
    (hd / "mylib").mkdir(parents=True)
    (hd / "handler.py").write_text(_ENTRY)
    (hd / "mylib" / "__init__.py").write_text(_SIBLING)

    port = _free_port()
    backend = SubprocessBackend()
    config = CustomConfig(class_name="MultiHandler", entry_file="handler.py")
    try:
        # spawn() waits for /health and raises if the process crashes — so a
        # successful return already proves the sibling import resolved at import time.
        await backend.spawn(
            agent_id=agent_id, name="Multi", description="d",
            handler_type="custom", config=config, port=port,
        )
        import httpx
        payload = {
            "jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": {
                "role": "user", "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "text", "text": "hello"}],
            }},
        }
        async with httpx.AsyncClient() as c:
            r = await c.post(f"http://localhost:{port}/", json=payload, timeout=15)
        assert "tagged:hello" in r.text  # sibling module's output round-tripped
    finally:
        await backend.stop(agent_id)
