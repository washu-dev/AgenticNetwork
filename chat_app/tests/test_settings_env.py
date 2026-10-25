"""Setting names: PLANNER_* and CHAT_APP_* first, the old names as fallbacks (chat_app/env.py)."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from chat_app import __main__ as entry_point
from chat_app import env, server
from chat_app.planner import runtime
from chat_app.planner.config import PlannerConfigError, load_config

ENV_LOGGER = "chat_app.env"
OLD_TO_NEW = [(old, new) for new, olds in env.RENAMED.items() for old in olds]

# (new name, value, config field, expected value) for every planner-loop setting.
LOOP_SETTINGS = [
    ("PLANNER_LLM_ENABLED", "false", "llm_enabled", False),
    ("PLANNER_LLM_PROVIDER", "Vertex", "llm_provider", "vertex"),
    ("PLANNER_LLM_MODEL", "some-model", "llm_model", "some-model"),
    ("PLANNER_LLM_MAX_OUTPUT_TOKENS", "4000", "max_output_tokens", 4000),
    ("PLANNER_LLM_TIMEOUT_SECONDS", "120", "timeout_seconds", 120.0),
    ("PLANNER_LLM_PRIMARY_TIMEOUT_SECONDS", "7", "llm_primary_timeout_seconds", 7.0),
    ("PLANNER_LLM_FALLBACK_PROVIDER", "Vertex", "llm_fallback_provider", "vertex"),
    ("PLANNER_LLM_FALLBACK_BASE_URL", "http://fallback/v1", "llm_fallback_base_url", "http://fallback/v1"),
    ("PLANNER_LLM_FALLBACK_MODEL", "fallback-model", "llm_fallback_model", "fallback-model"),
    ("PLANNER_MAX_STEPS", "6", "max_steps", 6),
    ("PLANNER_HISTORY_LIMIT", "5", "history_limit", 5),
    ("PLANNER_POLICY_MODE", "Strict", "policy_mode", "strict"),
]


@pytest.fixture(autouse=True)
def _fresh_notices():
    env.reset()
    yield
    env.reset()


def _clear(monkeypatch) -> None:
    for new, olds in env.RENAMED.items():
        for name in (new, *olds):
            monkeypatch.delenv(name, raising=False)


def _env_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == ENV_LOGGER]


# ── the reader ────────────────────────────────────────────────────────────────


def test_new_name_is_read(caplog) -> None:
    assert env.setting("PLANNER_LLM_MODEL", "default", env={"PLANNER_LLM_MODEL": "new"}) == "new"
    assert env.setting("PLANNER_LLM_MODEL", "default", env={}) == "default"
    with caplog.at_level(logging.WARNING, logger=ENV_LOGGER):
        env.log_old_names()
    assert _env_warnings(caplog) == []


@pytest.mark.parametrize("old,new", OLD_TO_NEW)
def test_old_name_is_a_fallback(old, new, caplog) -> None:
    assert env.setting(new, "default", env={old: "from-old"}) == "from-old"
    with caplog.at_level(logging.WARNING, logger=ENV_LOGGER):
        env.log_old_names()
    assert _env_warnings(caplog) == [f"{old} is deprecated; use {new}"]


def test_new_name_wins_when_both_are_set(caplog) -> None:
    values = {"PLANNER_LLM_MODEL": "new", "MAIN_AGENT_LLM_MODEL": "old"}
    assert env.setting("PLANNER_LLM_MODEL", env=values) == "new"
    with caplog.at_level(logging.WARNING, logger=ENV_LOGGER):
        env.log_old_names()
    assert _env_warnings(caplog) == [
        "PLANNER_LLM_MODEL and MAIN_AGENT_LLM_MODEL are both set, to different values; using PLANNER_LLM_MODEL"
    ]


def test_both_names_set_to_the_same_value_is_quiet(caplog) -> None:
    # Both names set to one value (an older .env alongside a newer one) is no conflict.
    assert env.setting("CHAT_APP_PORT", env={"CHAT_APP_PORT": "8190", "MAIN_AGENT_PORT": "8190"}) == "8190"
    with caplog.at_level(logging.WARNING, logger=ENV_LOGGER):
        env.log_old_names()
    assert _env_warnings(caplog) == []


@pytest.mark.parametrize("offset, expected", [(100, "http://localhost:8191"), (0, "")])
def test_submit_sbatch_points_a_test_chat_app_at_its_own_registration_service(offset, expected) -> None:
    # A test instance (PORT_OFFSET != 0) forwards /register/* to its own, offset
    # registration service; prod leaves the URL to .env or the chat app's default.
    sbatch = (Path(__file__).resolve().parents[2] / "deployment" / "submit.sbatch").read_text()
    ports = sbatch[sbatch.index("export REGISTRATION_SERVICE_PORT="):]
    ports = ports[:ports.index("\nfi\n", ports.index('if [ "$PORT_OFFSET" -ne 0 ]')) + 4]
    env_vars = {"PATH": os.environ["PATH"], "PORT_OFFSET": str(offset)}
    out = subprocess.run(
        ["bash", "-c", ports + 'printf %s "${REGISTRATION_SERVICE_URL:-}"'],
        env=env_vars, capture_output=True, text=True, check=True,
    )
    assert out.stdout == expected


def test_an_empty_new_name_still_counts_as_set() -> None:
    # Same rule as os.getenv: a present name is used even when empty, so parsing is unchanged.
    values = {"PLANNER_LLM_ENABLED": "", "MAIN_AGENT_LLM_ENABLED": "true"}
    assert env.setting("PLANNER_LLM_ENABLED", env=values) == ""
    assert load_config({"PLANNER_LLM_ENABLED": ""}).llm_enabled is False
    assert load_config({"MAIN_AGENT_LLM_ENABLED": ""}).llm_enabled is False


def test_one_warning_per_old_name(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=ENV_LOGGER):
        for _ in range(3):
            env.setting("PLANNER_MAX_STEPS", env={"MAIN_AGENT_MAX_STEPS": "4"})
        assert _env_warnings(caplog) == []  # nothing is logged before log_old_names()
        env.log_old_names()
        env.log_old_names()
        env.setting("PLANNER_MAX_STEPS", env={"MAIN_AGENT_MAX_STEPS": "4"})
        # After log_old_names(), a new old name is logged when it is read.
        env.setting("PLANNER_HISTORY_LIMIT", env={"MAIN_AGENT_HISTORY_LIMIT": "5"})
        env.setting("PLANNER_HISTORY_LIMIT", env={"MAIN_AGENT_HISTORY_LIMIT": "5"})
    assert _env_warnings(caplog) == [
        "MAIN_AGENT_MAX_STEPS is deprecated; use PLANNER_MAX_STEPS",
        "MAIN_AGENT_HISTORY_LIMIT is deprecated; use PLANNER_HISTORY_LIMIT",
    ]


def test_only_names_in_the_table_are_read() -> None:
    # Other CHAT_APP_* variables (image tags, service accounts) are not settings.
    with pytest.raises(KeyError):
        env.setting("CHAT_APP_IMAGE_TAG", env={"CHAT_APP_IMAGE_TAG": "dev-alice"})


# ── load_config (planner loop) ─────────────────────────────────────────────────


def test_loop_table_covers_every_planner_setting() -> None:
    assert {row[0] for row in LOOP_SETTINGS} == {n for n in env.RENAMED if n.startswith("PLANNER_")}


@pytest.mark.parametrize("use_old_name", [False, True], ids=["new-name", "old-name"])
@pytest.mark.parametrize("name,value,field,expected", LOOP_SETTINGS, ids=[row[0] for row in LOOP_SETTINGS])
def test_load_config_reads_each_planner_setting(name, value, field, expected, use_old_name) -> None:
    key = env.RENAMED[name][0] if use_old_name else name
    extra = {"PLANNER_LLM_FALLBACK_MODEL": "m"} if name == "PLANNER_LLM_FALLBACK_PROVIDER" else {}
    cfg = load_config({"OPENAI_API_KEY": "test-key", **extra, key: value})
    assert getattr(cfg, field) == expected


def test_config_errors_name_the_new_settings() -> None:
    with pytest.raises(PlannerConfigError, match="PLANNER_LLM_ENABLED=true"):
        load_config({"MAIN_AGENT_LLM_ENABLED": "true"})
    with pytest.raises(PlannerConfigError, match="PLANNER_LLM_FALLBACK_MODEL is missing"):
        load_config({"PLANNER_LLM_ENABLED": "false", "MAIN_AGENT_LLM_FALLBACK_PROVIDER": "vertex"})


# ── python -m chat_app (host, port, log level) ─────────────────────────────────


@pytest.mark.parametrize("host_name,port_name", [("CHAT_APP_HOST", "CHAT_APP_PORT"), ("MAIN_AGENT_HOST", "MAIN_AGENT_PORT")])
def test_entry_point_reads_host_and_port(monkeypatch, host_name, port_name) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(host_name, "127.0.0.2")
    monkeypatch.setenv(port_name, "8191")
    args = entry_point.parse_args([])
    assert (args.host, args.port) == ("127.0.0.2", 8191)


@pytest.mark.parametrize("name", ["CHAT_APP_LOG_LEVEL", "MAIN_AGENT_LOG_LEVEL"])
def test_entry_point_reads_the_log_level(monkeypatch, name) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(name, "debug")
    seen: dict = {}
    monkeypatch.setattr(entry_point.logging, "basicConfig", lambda **kwargs: seen.update(kwargs))
    entry_point._configure_logging()
    assert seen["level"] == logging.DEBUG


def test_entry_point_logs_old_names_after_logging_is_set_up(monkeypatch, caplog) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("MAIN_AGENT_PORT", "8191")
    monkeypatch.setenv("MAIN_AGENT_LLM_MODEL", "old-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    events: list = []
    monkeypatch.setattr(entry_point, "_configure_logging", lambda: events.append(("logging", _env_warnings(caplog))))
    monkeypatch.setattr(entry_point, "DirectoryClient", lambda **kwargs: object())
    monkeypatch.setattr(entry_point, "create_app", lambda **kwargs: object())
    fake_uvicorn = types.SimpleNamespace(run=lambda app, host, port: events.append(("run", host, port)))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    with caplog.at_level(logging.WARNING, logger=ENV_LOGGER):
        assert entry_point.main([]) == 0
    # Nothing was logged before logging was configured; the port came from the old name.
    assert events == [("logging", []), ("run", "0.0.0.0", 8191)]
    assert _env_warnings(caplog) == [
        "MAIN_AGENT_PORT is deprecated; use CHAT_APP_PORT",
        "MAIN_AGENT_LLM_MODEL is deprecated; use PLANNER_LLM_MODEL",
    ]


# ── server and runtime (uploads, card paths, anonymous access) ─────────────────


class _Upload:
    def __init__(self, data: bytes, filename: str = "note.txt") -> None:
        self.filename = filename
        self.content_type = "text/plain"
        self._data = data

    async def read(self) -> bytes:
        return self._data


@pytest.mark.parametrize("name", ["CHAT_APP_UPLOAD_DIR", "MAIN_AGENT_UPLOAD_DIR"])
def test_server_and_runtime_agree_on_the_upload_dir(tmp_path, monkeypatch, name) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(name, str(tmp_path / "uploads"))
    stored = asyncio.run(server._store_uploaded_file(_Upload(b"hello")))
    assert Path(stored["path"]).parent == tmp_path / "uploads"
    assert runtime._is_safe_uploaded_file(stored["path"])
    outside = tmp_path / "outside.txt"
    outside.write_text("not an upload", encoding="utf-8")
    assert not runtime._is_safe_uploaded_file(str(outside))


def test_new_upload_dir_wins_in_server_and_runtime(tmp_path, monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("CHAT_APP_UPLOAD_DIR", str(tmp_path / "new"))
    monkeypatch.setenv("MAIN_AGENT_UPLOAD_DIR", str(tmp_path / "old"))
    stored = asyncio.run(server._store_uploaded_file(_Upload(b"hello")))
    assert Path(stored["path"]).parent == tmp_path / "new"
    assert runtime._is_safe_uploaded_file(stored["path"])
    old_file = tmp_path / "old" / "note.txt"
    old_file.parent.mkdir()
    old_file.write_text("in the old dir", encoding="utf-8")
    assert not runtime._is_safe_uploaded_file(str(old_file))


@pytest.mark.parametrize("name", ["CHAT_APP_MAX_UPLOAD_BYTES", "MAIN_AGENT_MAX_UPLOAD_BYTES"])
def test_max_upload_bytes(tmp_path, monkeypatch, name) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("CHAT_APP_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setenv(name, "4")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(server._store_uploaded_file(_Upload(b"hello")))
    assert excinfo.value.status_code == 413


@pytest.mark.parametrize("name", ["CHAT_APP_CARD_PATHS", "MAIN_AGENT_CARD_PATHS"])
def test_card_paths(monkeypatch, name) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(name, "a.json, b.json")
    assert server._agent_card_paths(None, allow_local=True) == [Path("a.json"), Path("b.json")]
    assert server._agent_card_paths(None, allow_local=False) == []


@pytest.mark.parametrize("name", ["CHAT_APP_ALLOW_ANON", "PLANNER_ALLOW_ANON"])
def test_allow_anon_needs_exactly_1(monkeypatch, name) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    assert server._anon_identity_allowed() is False
    for value in ("true", "yes", "on", "0", "", " 1", "1 "):
        monkeypatch.setenv(name, value)
        assert server._anon_identity_allowed() is False, value
    monkeypatch.setenv(name, "1")
    assert server._anon_identity_allowed() is True


def test_new_allow_anon_name_wins(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    monkeypatch.setenv("CHAT_APP_ALLOW_ANON", "0")
    monkeypatch.setenv("PLANNER_ALLOW_ANON", "1")
    assert server._anon_identity_allowed() is False
