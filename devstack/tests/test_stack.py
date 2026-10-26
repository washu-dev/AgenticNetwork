"""Stack CLI behaviour that needs no running services (the live path is `up`/`smoke`)."""

from __future__ import annotations

import json
import os
import shlex
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
    assert list(status["services"]) == ["directory", "mock-llm", "registration", "chat-app", "proxy"]
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
    for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "PLANNER_LLM_MODEL", "MAIN_AGENT_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert stack.main(["up", "--llm", "openai", "--state-dir", str(tmp_path)]) == stack.EXIT_PREFLIGHT
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err and "PLANNER_LLM_MODEL" in err


def test_planner_model_reads_the_new_name_then_the_old_one(monkeypatch):
    for name in ("PLANNER_LLM_MODEL", "MAIN_AGENT_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert stack.planner_model_setting({}) == ""
    assert stack.planner_model_setting({"MAIN_AGENT_LLM_MODEL": "old"}) == "old"
    assert stack.planner_model_setting({"MAIN_AGENT_LLM_MODEL": "old", "PLANNER_LLM_MODEL": "new"}) == "new"
    monkeypatch.setenv("MAIN_AGENT_LLM_MODEL", "old-from-env")
    assert stack.planner_model_setting({}) == "old-from-env"
    assert stack.planner_model_setting({"PLANNER_LLM_MODEL": "new"}) == "new"  # a new name anywhere wins


def test_the_chat_app_gets_the_new_setting_names(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVSTACK_PASSTHROUGH_ENV", raising=False)
    cfg = stack.StackConfig(state_dir=tmp_path, planner_model="some-model")
    env = stack._service_specs(cfg)["chat-app"].env
    assert env["PLANNER_LLM_MODEL"] == "some-model"
    assert env["CHAT_APP_UPLOAD_DIR"] == str(tmp_path / "uploads")
    assert not any(name.startswith("MAIN_AGENT_") for name in env)


def test_the_chat_app_runs_from_its_package_and_the_dev_venv(tmp_path, monkeypatch):
    for name in ("CHAT_APP_PYTHON", "PLANNER_PYTHON", "REGISTRATION_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    dev_venv = str(stack.REPO / ".venv" / "bin" / "python")
    assert stack.chat_app_python() == stack.registration_python() == dev_venv  # one venv for both
    monkeypatch.setenv("PLANNER_PYTHON", "/old/python")
    assert stack.chat_app_python() == "/old/python"  # the older name still works
    monkeypatch.setenv("CHAT_APP_PYTHON", "/new/python")
    assert stack.chat_app_python() == "/new/python"  # and the new name wins
    spec = stack._service_specs(stack.StackConfig(state_dir=tmp_path))["chat-app"]
    assert spec.command[:3] == ["/new/python", "-m", "chat_app"]
    assert spec.marker in shlex.join(spec.command)


@pytest.mark.parametrize("script, args, expected", [
    ("scripts/ads", ["-list"], "-m agentnet.scripts.show_ads_routes"),
    ("chat_app/scripts/start.sh", [], "-m chat_app"),
])
def test_the_other_dev_scripts_default_to_the_dev_venv(tmp_path, script, args, expected):
    # A copy of the script in a scratch repo, with a dev venv and a leftover chat_app/.venv.
    copy = tmp_path / script
    copy.parent.mkdir(parents=True)
    shutil.copy2(stack.REPO / script, copy)
    for venv, name in ((".venv", "dev"), ("chat_app/.venv", "leftover")):
        python = tmp_path / venv / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\necho "{name} venv: $*"\n', encoding="utf-8")
        python.chmod(0o755)
    (tmp_path / "chat_app" / ".env.local").write_text("OPENAI_API_KEY=sk-test-placeholder\n", encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}  # no ADS_PYTHON or PYTHON_BIN
    result = subprocess.run(["bash", str(copy), *args], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert f"dev venv: {expected}" in result.stdout and "leftover" not in result.stdout


def test_preflight_checks_the_dev_venv_once_and_says_how_to_create_it(tmp_path, monkeypatch):
    for name in ("CHAT_APP_PYTHON", "PLANNER_PYTHON", "REGISTRATION_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(stack, "DEV_VENV_PYTHON", tmp_path / "absent" / "python")
    cfg = stack.StackConfig(state_dir=tmp_path)
    running = {name: 1 for name in cfg.services}  # so no port is checked
    [problem] = stack.preflight(cfg, running)
    assert problem.startswith(f"dev venv not found: {tmp_path / 'absent' / 'python'}.")
    assert stack.DEV_VENV_SETUP in problem and "CHAT_APP_PYTHON and REGISTRATION_PYTHON" in problem
    # An override splits the check per service, each naming its own variable.
    monkeypatch.setenv("CHAT_APP_PYTHON", str(tmp_path / "chat-python"))
    chat, registration = stack.preflight(cfg, running)
    assert chat.startswith("chat-app interpreter not found:") and chat.endswith("or set CHAT_APP_PYTHON.")
    assert registration.startswith("registration interpreter not found:") and registration.endswith("REGISTRATION_PYTHON.")


def test_both_services_pin_one_a2a_sdk():
    # The dev venv installs both requirement files, so a second version cannot resolve.
    pins = stack.a2a_pins()
    assert set(pins) == {"chat_app/requirements.txt", "registration_service/requirements.txt"}
    assert len(set(pins.values())) == 1 and None not in pins.values(), pins


def test_doctor_checks_the_dev_venv_against_the_one_a2a_pin(tmp_path, monkeypatch, capsys):
    for name in ("CHAT_APP_PYTHON", "PLANNER_PYTHON", "REGISTRATION_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(stack, "ENV_FILE", tmp_path / "absent.env")
    python = tmp_path / "python"  # answers stack.venv_report
    monkeypatch.setattr(stack, "DEV_VENV_PYTHON", python)
    [pin] = set(stack.a2a_pins().values())

    def doctor(a2a: str | None) -> dict[str, dict]:
        if a2a is None:
            python.unlink(missing_ok=True)
        else:
            packages = {"a2a-sdk": a2a, "fastapi": "1", "uvicorn": "1", "httpx": "1", "openai": "1"}
            python.write_text(f"#!/bin/sh\necho '{json.dumps({'python': '3.12.4', 'packages': packages})}'\n")
            python.chmod(0o755)
        stack.main(["doctor", "--json", "--state-dir", str(tmp_path / "state")])
        return {c["check"]: c for c in json.loads(capsys.readouterr().out)["checks"]}

    checks = doctor(pin)
    assert [name for name in checks if "venv" in name or "interpreter" in name] == ["dev venv"]
    assert checks["dev venv"]["level"] == "ok" and f"a2a-sdk {pin}," in checks["dev venv"]["detail"]
    assert "a2a-sdk pin" not in checks
    checks = doctor("0.3.26")
    assert checks["dev venv a2a-sdk"]["level"] == "warn"
    assert checks["dev venv a2a-sdk"]["detail"].startswith(f"has 0.3.26, the services pin {pin}")
    checks = doctor(None)
    assert checks["dev venv"]["level"] == "fail" and stack.DEV_VENV_SETUP in checks["dev venv"]["detail"]


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


def listener(log: Path, marker: str, *, session: bool = False) -> tuple[subprocess.Popen, int]:
    """``session`` makes it a process-group leader, as `up` starts services."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as out:
        proc = subprocess.Popen([sys.executable, "-c", LISTENER, marker], stdout=out, start_new_session=session)
    deadline = time.monotonic() + 10
    while not log.read_text().strip():
        assert time.monotonic() < deadline, "listener did not start"
        time.sleep(0.05)
    return proc, int(log.read_text().split()[0])


def test_process_command_is_not_truncated():
    # Markers come at the end of long command lines (venv paths, agent entry points).
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "x" * 200, "end-marker"])
    try:
        assert stack.process_command(proc.pid).endswith("end-marker")
    finally:
        proc.kill()
        proc.wait()


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
def test_holder_logging_outside_any_state_dir_gets_a_kill_command(tmp_path):
    state, handrun = tmp_path.resolve() / "state", tmp_path.resolve() / "handrun"  # handrun has no marker
    proc, port = listener(handrun / "logs" / "mock-llm.log", "devstack.mock_llm")
    try:
        cfg = stack.StackConfig(state_dir=state, port_offset=port - stack.BASE_PORTS["mock-llm"])
        ours, foreign = stack.stray_services(cfg, "mock-llm", "devstack.mock_llm")
        assert ours == []
        assert [(f["pid"], f["state_dir"], f["stop_command"]) for f in foreign] == [(proc.pid, None, f"kill {proc.pid}")]
    finally:
        proc.kill()
        proc.wait()


@needs_lsof
def test_down_adopts_only_services_that_log_into_its_own_state_dir(tmp_path):
    state, other = tmp_path.resolve() / "state", tmp_path.resolve() / "other"
    stack.ensure_state_dir(stack.StackConfig(state_dir=other))  # another stack's state dir
    for log, owned in ((other / "logs" / "mock-llm.log", False), (state / "logs" / "mock-llm.log", True)):
        proc, port = listener(log, "devstack.mock_llm")
        try:
            cfg = stack.StackConfig(state_dir=state, port_offset=port - stack.BASE_PORTS["mock-llm"])
            ours, foreign = stack.stray_services(cfg, "mock-llm", "devstack.mock_llm")
            if owned:  # ours, but its pid file was lost
                assert (ours, foreign) == ([proc.pid], [])
            else:
                assert ours == [] and [(f["pid"], f["port"], f["state_dir"]) for f in foreign] == [
                    (proc.pid, port, str(other))
                ]
        finally:
            proc.kill()
            proc.wait()


@needs_lsof
def test_down_reports_but_never_stops_another_stacks_service(tmp_path, monkeypatch, capsys):
    state, other = tmp_path.resolve() / "state", tmp_path.resolve() / "other"
    stack.ensure_state_dir(stack.StackConfig(state_dir=other))
    proc, port = listener(other / "logs" / "mock-llm.log", "devstack.mock_llm", session=True)
    # The agent port range may run past 65535 at this offset; agent sweeping has its own test.
    monkeypatch.setattr(stack, "sweep_agents", lambda cfg: [])
    try:
        cfg = stack.StackConfig(state_dir=state, port_offset=port - stack.BASE_PORTS["mock-llm"])
        stack.ensure_state_dir(cfg)
        cfg.config_file.write_text(json.dumps(cfg.saved()), encoding="utf-8")
        assert stack.main(["down", "--json", "--state-dir", str(state)]) == stack.EXIT_NOT_READY
        assert proc.poll() is None  # still running
        out = capsys.readouterr()
        report = json.loads(out.out)
        stop_command = f"scripts/stack down --state-dir {shlex.quote(str(other))}"
        holder = {"service": "mock-llm", "pid": proc.pid, "port": port, "state_dir": str(other)}
        assert report["ok"] is False and report["foreign"] == [{**holder, "stop_command": stop_command}]
        assert f"pid {proc.pid}" in out.err and stop_command in out.err
        # Its ports are not free, so reset deletes nothing either.
        assert stack.main(["reset", "--state-dir", str(state)]) == stack.EXIT_NOT_READY
        assert proc.poll() is None and cfg.config_file.exists()
    finally:
        proc.kill()
        proc.wait()


@needs_lsof
def test_failed_shutdown_keeps_recovery_state(tmp_path, monkeypatch):
    """A service that will not stop keeps its pid file, its log and the stack's config, and
    reset deletes nothing; once it stops, reset removes only what the stack created."""
    state = tmp_path.resolve() / "state"
    cfg = stack.StackConfig(state_dir=state, port_offset=13000)  # ports below the usual ephemeral ranges
    stack.ensure_state_dir(cfg)
    cfg.config_file.write_text(json.dumps(cfg.saved()), encoding="utf-8")
    (state / "notes.txt").write_text("keep me", encoding="utf-8")
    proc, _ = listener(cfg.log_file("mock-llm"), "devstack.mock_llm", session=True)
    cfg.pid_file("mock-llm").write_text(json.dumps({"pid": proc.pid}), encoding="utf-8")
    stop_pid = stack.stop_pid
    try:
        monkeypatch.setattr(stack, "stop_pid", lambda *args, **kwargs: False)  # it survives SIGKILL
        for command in ("down", "reset"):
            assert stack.main([command, "--state-dir", str(state)]) == stack.EXIT_NOT_READY
            assert proc.poll() is None
            assert cfg.config_file.exists() and cfg.pid_file("mock-llm").exists() and cfg.log_file("mock-llm").exists()
        monkeypatch.setattr(stack, "stop_pid", stop_pid)
        assert stack.main(["reset", "--state-dir", str(state)]) == stack.EXIT_OK
        assert proc.poll() is not None
        assert sorted(p.name for p in state.iterdir()) == [stack.STATE_MARKER, "notes.txt"]
    finally:
        proc.kill()
        proc.wait()


def test_registration_calls_carry_the_dev_identity(monkeypatch, tmp_path) -> None:
    """The stack's agents get the dev user as owner, so its own restarts pass the
    registration service's owner check (enforced in the Compose stack)."""
    cfg = stack.StackConfig(state_dir=tmp_path, dev_user="dev@example.test")
    sent: list[tuple[str, str, dict]] = []

    def fake_request(method, url, *, body=None, headers=None, timeout=5.0):
        sent.append((method, url.rsplit("/", 1)[-1], dict(headers or {})))
        if method == "POST" and url.endswith("/agents"):
            return 201, json.dumps({"id": "a1", "ads_cid": "cid"}).encode()
        return 200, b"{}"

    monkeypatch.setattr(stack, "http_request", fake_request)
    stack.register_agent(cfg, {"name": "Echo"})
    stack.restart_agent(cfg, {"id": "a1", "name": "Echo"})
    assert sent == [
        ("POST", "agents", {"mail": "dev@example.test"}),
        ("POST", "restart", {"mail": "dev@example.test"}),
    ]
