"""Stack CLI behaviour that needs no running services (the live path is `up`/`smoke`)."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from devstack import stack

needs_lsof = pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof")
# A loopback listener that prints its port; its last argv word is a process marker.
LISTENER = (
    "import socket, time; s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); "
    "print(s.getsockname()[1], flush=True); time.sleep(60)"
)


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
    assert status["agents"] == [] and status["unreachable_agents"] == []


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


def test_up_refuses_a_non_empty_dir_that_is_not_a_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(stack, "ENV_FILE", tmp_path / "absent.env")
    (tmp_path / "precious.txt").write_text("keep me", encoding="utf-8")
    assert stack.main(["up", "--state-dir", str(tmp_path)]) == stack.EXIT_USAGE
    assert [p.name for p in tmp_path.iterdir()] == ["precious.txt"]


def test_reset_deletes_only_what_the_stack_created(tmp_path):
    state = tmp_path / "state"
    stack.ensure_state_dir(stack.StackConfig(state_dir=state))
    (state / "notes").mkdir()
    (state / "notes" / "mine.txt").write_text("keep me", encoding="utf-8")
    assert run_cli("reset", "--state-dir", str(state)).returncode == stack.EXIT_OK
    # The marker stays with the kept files, so the dir is still usable by `up` and `reset`.
    assert sorted(p.name for p in state.iterdir()) == [stack.STATE_MARKER, "notes"]
    assert (state / "notes" / "mine.txt").exists()
    assert run_cli("reset", "--state-dir", str(state)).returncode == stack.EXIT_OK

    clean = tmp_path / "clean"
    stack.ensure_state_dir(stack.StackConfig(state_dir=clean))
    assert run_cli("reset", "--state-dir", str(clean)).returncode == stack.EXIT_OK
    assert not clean.exists()

    empty = tmp_path / "empty"  # e.g. a pre-created PIPELINE_STATE_DIR
    empty.mkdir()
    assert run_cli("reset", "--state-dir", str(empty)).returncode == stack.EXIT_OK


def test_down_without_stack_json_touches_no_ports(tmp_path):
    result = run_cli("down", "--state-dir", str(tmp_path))
    assert result.returncode == stack.EXIT_OK and "no stack.json" in result.stderr


def test_listed_agent_that_does_not_answer_is_unreachable():
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))  # bound, not listening: connections are refused
        listed = [{"name": "Echo", "endpoint": f"http://localhost:{closed.getsockname()[1]}/"}]
        assert stack.unreachable_agents(listed, ["echo", "other"]) == ["Echo"]


def listener(log: Path, marker: str) -> tuple[subprocess.Popen, int]:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as out:
        proc = subprocess.Popen([sys.executable, "-c", LISTENER, marker], stdout=out)
    deadline = time.monotonic() + 10
    while not log.read_text().strip():
        assert time.monotonic() < deadline, "listener did not start"
        time.sleep(0.05)
    return proc, int(log.read_text().split()[0])


@needs_lsof
@pytest.mark.parametrize("own", [True, False])
def test_sweep_stops_only_this_stacks_agents(tmp_path, own):
    cfg = stack.StackConfig(state_dir=tmp_path.resolve() / "state")
    logs = cfg.agent_logs_dir if own else tmp_path.resolve() / "other" / "logs" / "agents"
    proc, port = listener(logs / "agent.log", "registration_service/runner.py")
    try:
        cfg.port_offset = port - stack.AGENT_PORT_START
        if stack.AGENT_PORT_END + cfg.port_offset > 65535:
            pytest.skip("the OS picked a port this check cannot express as an offset")
        assert stack.sweep_agents(cfg) == []
        assert (proc.poll() is not None) == own  # another stack's agent is left alone
    finally:
        proc.kill()
        proc.wait()


@needs_lsof
def test_down_finds_a_service_started_from_another_state_dir(tmp_path):
    other = tmp_path.resolve() / "other"
    proc, port = listener(other / "logs" / "mock-llm.log", "devstack.mock_llm")
    try:
        cfg = stack.StackConfig(state_dir=tmp_path.resolve() / "state", port_offset=port - stack.BASE_PORTS["mock-llm"])
        assert stack.stray_services(cfg, "mock-llm", "devstack.mock_llm") == []  # not a state dir
        (other / stack.STATE_MARKER).touch()
        assert stack.stray_services(cfg, "mock-llm", "devstack.mock_llm") == [(proc.pid, other)]
    finally:
        proc.kill()
        proc.wait()
