"""Stack CLI behaviour that needs no running services (the live path is `up`/`smoke`)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys

from devstack import stack


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "devstack", *args], cwd=stack.REPO, capture_output=True, text=True, timeout=60
    )


def test_status_json_shape_when_nothing_runs(tmp_path):
    result = run_cli("status", "--json", "--state-dir", str(tmp_path))
    assert result.returncode == stack.EXIT_NOT_READY
    status = json.loads(result.stdout)
    assert status["ready"] is False and status["mode"] == "native" and status["llm"] == "mock"
    assert status["state_dir"] == str(tmp_path.resolve())
    assert list(status["services"]) == ["directory", "mock-llm", "registration", "planner", "proxy"]
    for service in status["services"].values():
        assert {"pid", "running", "ready", "url", "log"} <= set(service)
        assert service["running"] is False
    assert status["agents"] == []


def test_up_fails_preflight_on_a_busy_port(tmp_path, monkeypatch):
    monkeypatch.setattr(stack, "ENV_FILE", tmp_path / "absent.env")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        offset = port - stack.BASE_PORTS["proxy"]
        if offset < 0 or stack.AGENT_PORT_END + offset > 65535:
            return  # the OS picked a port this check cannot express as an offset
        code = stack.main(["up", "--state-dir", str(tmp_path / "state"), "--port-offset", str(offset)])
    assert code == stack.EXIT_PREFLIGHT
    assert not (tmp_path / "state").exists()  # nothing was started or written


def test_openai_mode_needs_its_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(stack, "ENV_FILE", tmp_path / "absent.env")
    for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "MAIN_AGENT_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert stack.main(["up", "--llm", "openai", "--state-dir", str(tmp_path)]) == stack.EXIT_PREFLIGHT
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_env_file_parsing(tmp_path):
    env_file = tmp_path / ".devstack.env"
    env_file.write_text(
        "# comment\nexport DEVSTACK_LLM=openai\nOPENAI_BASE_URL='http://127.0.0.1:11434/v1'\nBROKEN\n",
        encoding="utf-8",
    )
    assert stack.read_env_file(env_file) == {
        "DEVSTACK_LLM": "openai",
        "OPENAI_BASE_URL": "http://127.0.0.1:11434/v1",
    }


def test_reset_refuses_a_directory_that_is_not_a_state_dir(tmp_path):
    (tmp_path / "precious.txt").write_text("keep me", encoding="utf-8")
    result = run_cli("reset", "--state-dir", str(tmp_path))
    assert result.returncode == stack.EXIT_USAGE
    assert (tmp_path / "precious.txt").exists()
