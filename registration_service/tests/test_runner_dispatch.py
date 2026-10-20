"""runner.py hands each handler type to the right handler module, with its config.

The runner is the entry point of every locally spawned agent. It reads the
AGENT_CONFIG_JSON envelope and imports its handler module inside ``main()``, so a
wrong module name or a dropped config field shows up only when an agent starts.
These tests drive ``runner.main()`` with an envelope for each canonical handler
type and each legacy input name, with every handler module's ``run`` replaced by
a recorder, so no server starts.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("a2a")
pytest.importorskip("uvicorn")

from registration_service.backend import _config_envelope  # noqa: E402
from registration_service.handlers import (  # noqa: E402
    custom_handler,
    llm_handler,
    llm_http_handler,
    llm_upload_handler,
    proxy_handler,
)
from registration_service.models import OperationSpec  # noqa: E402
from registration_service.supervisor import _deserialize_config  # noqa: E402

_HANDLERS = (proxy_handler, llm_handler, llm_upload_handler, llm_http_handler, custom_handler)
_HANDLER_FILE = "/uploads/a1/agent.py"
_UPSTREAM = "http://upstream.example.test/a2a"
_BASE_URL = "https://api.example.test"
_OPERATIONS = [{"name": "get_x", "method": "GET", "path": "/x"}]
_DUMPED_OPERATIONS = [OperationSpec(**op).model_dump() for op in _OPERATIONS]
# Every handler gets these, whatever its type.
_COMMON = {"agent_id": "a1", "name": "Agent One", "description": "d", "port": 22001}


@pytest.fixture
def calls(monkeypatch) -> list[tuple[str, dict]]:
    """Replace each handler module's ``run`` with a recorder of (module, kwargs)."""
    recorded: list[tuple[str, dict]] = []
    for module in _HANDLERS:
        short_name = module.__name__.rsplit(".", 1)[-1]
        monkeypatch.setattr(
            module, "run", lambda _module=short_name, **kwargs: recorded.append((_module, kwargs))
        )
    monkeypatch.setenv("HANDLER_FILE_PATH", _HANDLER_FILE)
    monkeypatch.delenv("AGENT_PARENT_PID", raising=False)
    return recorded


def _run(monkeypatch, envelope: str) -> None:
    from registration_service import runner

    monkeypatch.setenv("AGENT_CONFIG_JSON", envelope)
    runner.main()


def _envelope(handler_type: str, config: dict) -> str:
    return json.dumps({
        "agent_id": "a1", "handler_type": handler_type, "port": 22001,
        "config": {**config, "name": "Agent One", "description": "d"},
    })


_LLM = {"system_prompt": "p", "model": "m"}

_DISPATCH_CASES = {
    # Canonical names.
    "proxy": (
        "proxy", {"upstream_url": _UPSTREAM, "auth_header": "Bearer op"},
        "proxy_handler", {"upstream_url": _UPSTREAM, "auth_header": "Bearer op"},
    ),
    "llm-none": (
        "llm", _LLM,
        "llm_handler", {"system_prompt": "p", "model": "m", "preset_tools": []},
    ),
    "llm-presets": (
        "llm", {**_LLM, "tools": {"source": "presets", "names": ["calculator"]}},
        "llm_handler", {"system_prompt": "p", "model": "m", "preset_tools": ["calculator"]},
    ),
    "llm-raw": (
        # Raw schemas have no bodies, so the agent runs prompt-only.
        "llm", {**_LLM, "tools": {"source": "raw", "schemas": [{"type": "function"}]}},
        "llm_handler", {"system_prompt": "p", "model": "m", "preset_tools": []},
    ),
    "llm-upload": (
        "llm", {**_LLM, "tools": {"source": "upload", "entry_file": "agent.py", "extra": {"k": 1}}},
        "llm_upload_handler",
        {"handler_file_path": _HANDLER_FILE, "system_prompt": "p", "model": "m", "extra": {"k": 1}},
    ),
    "llm-http": (
        "llm", {"model": "m", "tools": {"source": "http", "base_url": _BASE_URL,
                                        "operations": _OPERATIONS, "auth_env": "API_TOKEN"}},
        "llm_http_handler",
        {"base_url": _BASE_URL, "operations": _DUMPED_OPERATIONS, "auth_env": "API_TOKEN",
         "auth_scheme": "Bearer", "system_prompt": None, "model": "m"},
    ),
    "custom": (
        "custom", {"class_name": "MyHandler", "extra": {"k": 1}, "max_runtime_seconds": 60},
        "custom_handler",
        {"handler_file_path": _HANDLER_FILE, "class_name": "MyHandler", "extra": {"k": 1},
         "max_runtime_seconds": 60.0, "credentials": []},
    ),
    # Legacy input names, in their old flat config shapes.
    "api_proxy": (
        "api_proxy", {"upstream_url": _UPSTREAM},
        "proxy_handler", {"upstream_url": _UPSTREAM, "auth_header": None},
    ),
    "tool_loop": (
        "tool_loop", {**_LLM, "entry_file": "agent.py", "extra": {"k": 1}},
        "llm_upload_handler",
        {"handler_file_path": _HANDLER_FILE, "system_prompt": "p", "model": "m", "extra": {"k": 1}},
    ),
    "endpoint_ops": (
        "endpoint_ops", {"base_url": _BASE_URL, "operations": _OPERATIONS, "auth_env": "API_TOKEN"},
        "llm_http_handler",
        {"base_url": _BASE_URL, "operations": _DUMPED_OPERATIONS, "auth_env": "API_TOKEN",
         "auth_scheme": "Bearer", "system_prompt": None},
    ),
}


@pytest.mark.parametrize(
    "handler_type, config, module, expected",
    list(_DISPATCH_CASES.values()),
    ids=list(_DISPATCH_CASES),
)
def test_runner_dispatches_to_the_right_handler(
    monkeypatch, calls, handler_type, config, module, expected
) -> None:
    _run(monkeypatch, _envelope(handler_type, config))

    assert [m for m, _ in calls] == [module]
    kwargs = calls[0][1]
    for key, value in {**_COMMON, **expected}.items():
        assert kwargs[key] == value, key


# ── Stored legacy rows, local subprocess path ───────────────────────────────────
# The supervisor reads a stored row with _deserialize_config (legacy names and old
# shapes are normalized on read) and passes the backend the row's own handler_type
# with that typed config. The envelope must carry the canonical name with it: a
# legacy name made the runner read the canonical payload with the legacy-shape
# reader, so a `tool_loop` row lost its `extra` and an `endpoint_ops` row failed
# to start.

_LEGACY_ROWS = {
    "tool_loop": (
        {"system_prompt": "p", "model": "m", "entry_file": "agent.py", "requirements": [],
         "install_project": True, "extra": {"k": 1}, "python_version": "3.11",
         "system_packages": []},
        "llm_upload_handler", {"system_prompt": "p", "model": "m", "extra": {"k": 1}},
    ),
    "endpoint_ops": (
        {"base_url": _BASE_URL, "auth_env": "API_TOKEN", "operations": _OPERATIONS},
        "llm_http_handler",
        {"base_url": _BASE_URL, "operations": _DUMPED_OPERATIONS, "auth_env": "API_TOKEN"},
    ),
    "api_proxy": (
        {"upstream_url": _UPSTREAM, "auth_header": None},
        "proxy_handler", {"upstream_url": _UPSTREAM, "auth_header": None},
    ),
}


@pytest.mark.parametrize(
    "stored_type, stored_config, module, expected",
    [(t, *case) for t, case in _LEGACY_ROWS.items()],
    ids=list(_LEGACY_ROWS),
)
def test_stored_legacy_row_starts_on_the_local_path(
    monkeypatch, calls, stored_type, stored_config, module, expected
) -> None:
    row = {"handler_type": stored_type, "config": stored_config}
    envelope = _config_envelope(
        "a1", "Agent One", "d", stored_type, _deserialize_config(row), 22001
    )

    _run(monkeypatch, envelope)

    assert [m for m, _ in calls] == [module]
    kwargs = calls[0][1]
    for key, value in {**_COMMON, **expected}.items():
        assert kwargs[key] == value, key
