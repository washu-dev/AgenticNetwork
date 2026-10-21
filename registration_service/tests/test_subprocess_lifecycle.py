"""SubprocessBackend process hygiene: logs, sessions, readiness, cleanup.

Spawns REAL agent processes. Each test checks one property the one-command dev
stack relies on: agent output lands in a per-agent log file (no undrained
pipes), agents run in their own session and import the repo without the
caller's PYTHONPATH, readiness means "serves its agent card" (not "anything
answers HTTP"), a failed spawn leaves nothing running and frees its port, an
install step that does not end is killed, and nothing outlives the service —
not on shutdown, not when it is SIGKILLed.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytest.importorskip("a2a")
pytest.importorskip("uvicorn")

from registration_service.backend import _REPO_ROOT, SubprocessBackend  # noqa: E402
from registration_service.models import CustomConfig, ProxyConfig  # noqa: E402
from registration_service.port_pool import port_free_on_host  # noqa: E402
from registration_service.tests.agent_stubs import free_port, pid_alive  # noqa: E402


_PROXY = ProxyConfig(upstream_url="http://127.0.0.1:9/")  # never called here
_RUNNER = str(_REPO_ROOT / "registration_service" / "runner.py")


def _wait_gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


async def _spawn_proxy(backend: SubprocessBackend, agent_id: str | None = None, port: int | None = None):
    agent_id = agent_id or str(uuid.uuid4())
    port = port or free_port()
    await backend.spawn(
        agent_id=agent_id, name="Lifecycle", description="d",
        handler_type="proxy", config=_PROXY, port=port,
    )
    return agent_id, port, backend._procs[agent_id].pid


async def test_spawn_logs_to_file_in_own_session_without_caller_pythonpath(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    backend = SubprocessBackend()
    agent_id, port, pid = await _spawn_proxy(backend)
    try:
        assert os.getpgid(pid) == pid != os.getpgrp()  # own session / group
        log = tmp_path / "logs" / f"{agent_id}.log"
        assert f"--- spawn agent_id={agent_id} port={port}" in log.read_text()
        assert "runner_start" in log.read_text()
    finally:
        await backend.stop(agent_id)
    assert _wait_gone(pid)
    assert port_free_on_host(port)


async def test_crash_during_startup_reports_this_runs_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path / "handlers"))
    agent_id = str(uuid.uuid4())
    handler_dir = tmp_path / "handlers" / agent_id
    handler_dir.mkdir(parents=True)
    (handler_dir / "handler.py").write_text("raise RuntimeError('boom-marker')\n")

    backend = SubprocessBackend()
    with pytest.raises(RuntimeError) as err:
        await backend.spawn(
            agent_id=agent_id, name="Crashy", description="d", handler_type="custom",
            config=CustomConfig(class_name="Nope", entry_file="handler.py"), port=free_port(),
        )
    message = str(err.value)
    assert "exited with code" in message
    assert "boom-marker" in message
    assert f"{agent_id}.log" in message
    assert backend._procs == {}


_SQUATTER = textwrap.dedent('''
    # Serves 404 for everything on the agent's port and never becomes an A2A app;
    # also starts a grandchild to prove the whole process group is stopped.
    import json, os, subprocess
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port = json.loads(os.environ["AGENT_CONFIG_JSON"])["port"]
    grandchild = subprocess.Popen(["sleep", "60"])
    with open({pidfile!r}, "w") as fh:
        fh.write(f"{{os.getpid()}} {{grandchild.pid}}")
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()
        def log_message(self, *args):
            pass
    HTTPServer(("0.0.0.0", port), H).serve_forever()
''')


async def test_404_listener_is_not_ready_and_timeout_kills_group_and_frees_port(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path / "handlers"))
    monkeypatch.setenv("AGENT_SPAWN_HEALTH_TIMEOUT_SECONDS", "2")
    agent_id = str(uuid.uuid4())
    pidfile = tmp_path / "pids"
    handler_dir = tmp_path / "handlers" / agent_id
    handler_dir.mkdir(parents=True)
    (handler_dir / "handler.py").write_text(_SQUATTER.format(pidfile=str(pidfile)))
    port = free_port()

    backend = SubprocessBackend()
    with pytest.raises(RuntimeError, match=r"did not serve /\.well-known/agent-card\.json within 2s"):
        await backend.spawn(
            agent_id=agent_id, name="Squatter", description="d", handler_type="custom",
            config=CustomConfig(class_name="Nope", entry_file="handler.py"), port=port,
        )
    child, grandchild = (int(p) for p in pidfile.read_text().split())
    assert _wait_gone(child)
    assert _wait_gone(grandchild)
    assert port_free_on_host(port)
    assert backend._procs == {}


async def test_spawning_a_running_agent_again_replaces_it(tmp_path) -> None:
    backend = SubprocessBackend()
    agent_id, port, first_pid = await _spawn_proxy(backend)
    try:
        _, _, second_pid = await _spawn_proxy(backend, agent_id=agent_id, port=port)
        assert second_pid != first_pid
        assert _wait_gone(first_pid)
    finally:
        await backend.stop(agent_id)


async def test_stop_all_terminates_every_child() -> None:
    backend = SubprocessBackend()
    a1, port1, pid1 = await _spawn_proxy(backend)
    a2, port2, pid2 = await _spawn_proxy(backend)
    stopped = await backend.stop_all()
    assert sorted(stopped) == sorted([a1, a2])
    assert backend._procs == {}
    assert _wait_gone(pid1) and _wait_gone(pid2)
    assert port_free_on_host(port1) and port_free_on_host(port2)


async def test_agent_exits_when_the_service_is_sigkilled(tmp_path) -> None:
    """An agent runs in its own session, so a SIGKILLed service signals nobody;
    the runner's parent watch must make it exit on its own."""
    port = free_port()
    envelope = {
        "agent_id": str(uuid.uuid4()), "handler_type": "proxy", "port": port,
        "config": {"upstream_url": "http://127.0.0.1:9/", "name": "Orphan", "description": "d"},
    }
    fake_service = textwrap.dedent(f'''
        import os, subprocess, sys, time
        env = dict(os.environ, AGENT_PARENT_PID=str(os.getpid()))
        log = open({str(tmp_path / "agent.log")!r}, "ab")
        p = subprocess.Popen([sys.executable, {_RUNNER!r}], env=env, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        print(p.pid, flush=True)
        time.sleep(120)
    ''')
    env = dict(os.environ, AGENT_CONFIG_JSON=json.dumps(envelope), PYTHONPATH=str(_REPO_ROOT))
    service = subprocess.Popen(
        [sys.executable, "-c", fake_service], env=env, stdout=subprocess.PIPE, text=True
    )
    agent_pid = None
    try:
        agent_pid = int(service.stdout.readline())
        async with httpx.AsyncClient() as client:
            for _ in range(150):
                try:
                    r = await client.get(f"http://127.0.0.1:{port}/.well-known/agent-card.json")
                    if r.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail("agent never became ready: " + (tmp_path / "agent.log").read_text())
        service.send_signal(signal.SIGKILL)
        service.wait()
        assert _wait_gone(agent_pid), "orphaned agent kept running after its service died"
        assert "runner_parent_exited" in (tmp_path / "agent.log").read_text()
    finally:
        if service.poll() is None:
            service.kill()
            service.wait()
        if agent_pid and pid_alive(agent_pid):
            os.killpg(agent_pid, signal.SIGKILL)


def _hanging_install_step(tmp_path: Path) -> tuple[list[str], Path]:
    """A stand-in for a pip run that never ends and has started a child of its
    own; it writes both PIDs to a file once running."""
    pids = tmp_path / "pids"
    script = textwrap.dedent(f'''
        import os, subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{child.pid}}")
        time.sleep(60)
    ''')
    return [sys.executable, "-c", script], pids


async def _pids_once_running(pids: Path) -> list[int]:
    for _ in range(100):
        if pids.exists() and pids.read_text():
            return [int(p) for p in pids.read_text().split()]
        await asyncio.sleep(0.05)
    raise AssertionError("install step never started")


async def test_an_install_step_past_its_time_limit_is_killed_and_raises(
    monkeypatch, tmp_path
) -> None:
    # A local spawn installs holding the agent's lock, so each install step is
    # bounded (AGENT_INSTALL_TIMEOUT_SECONDS), and nothing it started outlives it.
    from registration_service import backend as backend_mod

    monkeypatch.setenv("AGENT_INSTALL_TIMEOUT_SECONDS", "1")
    cmd, pids = _hanging_install_step(tmp_path)

    # Ends promptly: a child left holding the output pipe would keep it waiting.
    with pytest.raises(RuntimeError, match="timed out after 1s"):
        await asyncio.wait_for(backend_mod._run(cmd), 10)

    for pid in await _pids_once_running(pids):
        assert _wait_gone(pid, 5)


async def test_a_cancelled_install_step_leaves_no_process(tmp_path) -> None:
    # Shutdown cancels a health-loop restart that may be installing.
    from registration_service import backend as backend_mod

    cmd, pids = _hanging_install_step(tmp_path)
    task = asyncio.create_task(backend_mod._run(cmd))
    running = await _pids_once_running(pids)

    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=10)
    assert done and task.cancelled()

    for pid in running:
        assert _wait_gone(pid, 5)


def _install_step_with_detached_child(tmp_path: Path, then: str) -> tuple[list[str], Path]:
    """A stand-in for a pip run that starts a child in a session of its own
    (outside the step's process group, still holding the step's output open),
    writes the child's PID to a file, then runs ``then``."""
    pid_file = tmp_path / "detached"
    script = textwrap.dedent(f'''
        import subprocess, sys, time
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
        )
        open({str(pid_file)!r}, "w").write(str(child.pid))
        {then}
    ''')
    return [sys.executable, "-c", script], pid_file


@pytest.mark.parametrize("then, raises", [
    ("pass", None),                      # the step succeeds at once
    ("time.sleep(60)", "timed out"),     # the step is past its time limit
])
async def test_an_install_step_ends_with_its_own_process(monkeypatch, tmp_path, then, raises) -> None:
    # A child that left the step's group outlives the kill and keeps the
    # output open; the step's result does not wait for it.
    from registration_service import backend as backend_mod

    monkeypatch.setenv("AGENT_INSTALL_TIMEOUT_SECONDS", "2")
    cmd, pid_file = _install_step_with_detached_child(tmp_path, then)
    try:
        if raises:
            with pytest.raises(RuntimeError, match=raises):
                await asyncio.wait_for(backend_mod._run(cmd), 5)
        else:
            await asyncio.wait_for(backend_mod._run(cmd), 5)
    finally:
        if pid_file.exists() and pid_file.read_text():
            os.kill(int(pid_file.read_text()), signal.SIGKILL)


async def test_a_failed_install_step_reports_the_end_of_its_output() -> None:
    from registration_service import backend as backend_mod

    cmd = [sys.executable, "-c", "print('x' * 3000); print('pip: no matching distribution'); raise SystemExit(3)"]
    with pytest.raises(RuntimeError, match=r"command failed \(3\)") as failed:
        await backend_mod._run(cmd)
    assert str(failed.value).endswith("pip: no matching distribution\n")


async def test_installing_the_project_leaves_the_stored_tree_as_uploaded(monkeypatch, tmp_path) -> None:
    """pip builds a project in the directory it installs from (setuptools writes
    build/ and *.egg-info/ there). The install runs from a copy, so the stored
    tree, and the revision an admin approved, stay what was uploaded."""
    from registration_service import backend as backend_mod
    from registration_service.archive import list_tree, tree_revision
    from registration_service.models import upload_spec

    handler_dir = tmp_path / "agent"
    (handler_dir / "mypkg").mkdir(parents=True)
    (handler_dir / "handler.py").write_text("x = 1\n")
    (handler_dir / "mypkg" / "__init__.py").write_text("")
    (handler_dir / "pyproject.toml").write_text("[project]\nname = 'mypkg'\nversion = '0.1'\n")
    (handler_dir / ".venv" / "bin").mkdir(parents=True)
    (handler_dir / ".venv" / "bin" / "python").write_text("")  # the venv exists
    before = tree_revision(handler_dir)
    installed: list[list[str]] = []

    async def pip_builds_in_tree(cmd: list[str]) -> None:
        project = Path(cmd[-1])
        installed.append(list_tree(project))
        (project / "mypkg.egg-info").mkdir()
        (project / "mypkg.egg-info" / "PKG-INFO").write_text("Name: mypkg\n")
        (project / "build" / "lib" / "mypkg").mkdir(parents=True)
        (project / "build" / "lib" / "mypkg" / "__init__.py").write_text("")

    monkeypatch.setattr(backend_mod, "_run", pip_builds_in_tree)
    spec = upload_spec(CustomConfig(class_name="H"))
    await SubprocessBackend()._ensure_venv(handler_dir, spec)

    assert installed == [["handler.py", "mypkg/__init__.py", "pyproject.toml"]]
    assert tree_revision(handler_dir) == before


async def test_a_spawn_cancelled_while_copying_the_project_leaves_no_copy(monkeypatch, tmp_path) -> None:
    """The copy runs in a thread, which a cancel does not stop. The cancelled
    install waits for it before removing the temporary directory, so the
    cancel propagates and no copy is left behind."""
    import shutil
    import tempfile
    import threading

    from registration_service import backend as backend_mod
    from registration_service.models import upload_spec

    handler_dir = tmp_path / "agent"
    (handler_dir / ".venv" / "bin").mkdir(parents=True)
    (handler_dir / ".venv" / "bin" / "python").write_text("")  # the venv exists
    (handler_dir / "handler.py").write_text("x = 1\n")
    (handler_dir / "pyproject.toml").write_text("[project]\nname = 'p'\nversion = '0.1'\n")
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    started, proceed, copied = threading.Event(), threading.Event(), threading.Event()
    copytree = shutil.copytree

    def slow_copytree(*args, **kwargs):
        started.set()
        proceed.wait(5)
        try:
            return copytree(*args, **kwargs)
        finally:
            copied.set()

    async def no_install(cmd: list[str]) -> None:
        pass

    monkeypatch.setattr(shutil, "copytree", slow_copytree)
    monkeypatch.setattr(backend_mod, "_run", no_install)
    spec = upload_spec(CustomConfig(class_name="H"))
    install = asyncio.create_task(SubprocessBackend()._ensure_venv(handler_dir, spec))
    assert await asyncio.to_thread(started.wait, 5)

    install.cancel()
    await asyncio.wait({install}, timeout=0.2)
    proceed.set()

    with pytest.raises(asyncio.CancelledError):
        await install
    assert await asyncio.to_thread(copied.wait, 5)
    assert list(temp_root.iterdir()) == []
