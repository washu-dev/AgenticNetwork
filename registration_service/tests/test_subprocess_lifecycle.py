"""SubprocessBackend process hygiene: logs, sessions, readiness, cleanup.

Spawns REAL agent processes. Each test checks one property the one-command dev
stack relies on: agent output lands in a per-agent log file (no undrained
pipes), agents run in their own session and import the repo without the
caller's PYTHONPATH, readiness means "serves its agent card" (not "anything
answers HTTP"), a failed spawn leaves nothing running and frees its port, and
nothing outlives the service — not on shutdown, not when it is SIGKILLed.
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
