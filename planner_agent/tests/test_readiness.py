"""GET /ready — per-dependency readiness (directory, db, llm), unlike /health."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from planner_agent.main_agent import db, server
from planner_agent.main_agent.config import MainAgentConfig
from agentnet.directory import backends
from agentnet.directory.backends import RealADSDirectoryBackend
from agentnet.directory.client import DirectoryClient
from planner_agent.main_agent.llm_client import ScriptedLLMClient
from planner_agent.main_agent.server import create_app
from planner_agent.tests.test_main_agent_runtime import FailA2A


@pytest.fixture(autouse=True)
def _no_configured_dependencies(monkeypatch) -> None:
    # Start every test from "nothing configured", whatever the caller's shell exports.
    for name in ("REGISTRATION_DB_DSN", "OPENAI_BASE_URL", "OPENAI_API_KEY", "DIRCTL_BIN", "ADS_BACKEND"):
        monkeypatch.delenv(name, raising=False)


def _ready(directory: DirectoryClient | None = None) -> httpx.Response:
    app = create_app(
        config=MainAgentConfig(llm_enabled=False, llm_model="mock", fake_ads=True),
        directory_client=directory or DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        llm_client=ScriptedLLMClient([]),
    )

    async def get() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            return await client.get("/ready")

    return asyncio.run(get())


def _real_directory(**kwargs) -> DirectoryClient:
    backend = RealADSDirectoryBackend(ads_url="127.0.0.1:8888", **kwargs)
    return DirectoryClient(fake_ads=False, backend=backend)


@contextlib.contextmanager
def _llm_server(status: int):
    """A local stand-in for an OpenAI-compatible server; yields (base_url, seen_paths)."""
    seen: list[tuple[str, bool]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append((self.path, bool(self.headers.get("Authorization"))))
            body = b'{"object": "list", "data": [{"id": "mock"}]}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1", seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_ready_with_fake_directory_and_nothing_else_configured() -> None:
    resp = _ready()
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ready": True,
        "checks": {
            "directory": {"status": "ok", "backend": "fake"},
            "db": {"status": "not_configured"},
            "llm": {"status": "not_configured"},
        },
    }


def test_ready_checks_real_cli_directory_with_dirctl() -> None:
    commands: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    resp = _ready(_real_directory(runner=runner))
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"]["directory"] == {"status": "ok", "backend": "real"}
    assert commands and commands[0][:3] == ["dirctl", "--server-addr", "127.0.0.1:8888"]


def test_ready_fails_when_dirctl_is_missing() -> None:
    resp = _ready(_real_directory(dirctl_path="/nonexistent/dirctl"))
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["ready"] is False
    check = body["checks"]["directory"]
    assert check["status"] == "error"
    assert "dirctl is not installed" in check["error"]


def test_ready_fails_when_dirctl_command_fails() -> None:
    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="connection refused")

    resp = _ready(_real_directory(runner=runner))
    assert resp.status_code == 503, resp.text
    assert "connection refused" in resp.json()["checks"]["directory"]["error"]


def test_ready_directory_check_times_out(monkeypatch) -> None:
    monkeypatch.setattr(server, "_READY_DIRECTORY_TIMEOUT_SECONDS", 0.05)

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        time.sleep(0.2)
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    resp = _ready(_real_directory(runner=runner))
    assert resp.status_code == 503, resp.text
    assert resp.json()["checks"]["directory"]["error"] == "timed out after 0.05s"


def test_ready_kills_hung_dirctl(tmp_path, monkeypatch) -> None:
    # The default runner bounds dirctl itself, so a hung daemon cannot leave a
    # blocked thread and a dirctl process behind on every /ready probe.
    monkeypatch.setattr(backends, "_HEALTHCHECK_TIMEOUT_SECONDS", 0.2)
    dirctl = tmp_path / "dirctl"
    dirctl.write_text("#!/bin/sh\nexec sleep 30\n")
    dirctl.chmod(0o755)
    started = time.monotonic()
    resp = _ready(_real_directory(dirctl_path=str(dirctl)))
    assert time.monotonic() - started < 3
    assert resp.status_code == 503, resp.text
    assert "dirctl command timed out after 0.2s" in resp.json()["checks"]["directory"]["error"]


def test_dirctl_timeout_defaults_to_env(monkeypatch) -> None:
    timeouts: list[float] = []

    def fake_run(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setenv("DIRCTL_TIMEOUT_SECONDS", "7")
    monkeypatch.setattr(subprocess, "run", fake_run)
    RealADSDirectoryBackend(ads_url="127.0.0.1:8888")._run_dirctl(["routing", "list"])
    assert timeouts == [7.0]


def test_ready_reports_reachable_llm(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with _llm_server(200) as (base_url, seen):
        monkeypatch.setenv("OPENAI_BASE_URL", base_url)
        resp = _ready()
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"]["llm"] == {
        "status": "ok",
        "url": f"{base_url}/models",
        "status_code": 200,
    }
    # The probe authenticates like the OpenAI SDK would.
    assert seen == [("/v1/models", True)]


def test_ready_treats_llm_auth_rejection_as_reachable(monkeypatch) -> None:
    with _llm_server(401) as (base_url, _):
        monkeypatch.setenv("OPENAI_BASE_URL", base_url)
        resp = _ready()
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"]["llm"]["status_code"] == 401


def test_ready_fails_when_llm_returns_5xx(monkeypatch) -> None:
    with _llm_server(503) as (base_url, _):
        monkeypatch.setenv("OPENAI_BASE_URL", base_url)
        resp = _ready()
    assert resp.status_code == 503, resp.text
    check = resp.json()["checks"]["llm"]
    assert check["status"] == "error"
    assert check["status_code"] == 503


def test_ready_fails_when_llm_unreachable(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{_closed_port()}/v1/")
    resp = _ready()
    assert resp.status_code == 503, resp.text
    check = resp.json()["checks"]["llm"]
    assert check["status"] == "error"
    assert check["url"].endswith("/v1/models")
    assert check["error"]


def test_ready_fails_when_db_pool_is_missing(monkeypatch) -> None:
    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.setattr(db, "_pool", None)
    resp = _ready()
    assert resp.status_code == 503, resp.text
    check = resp.json()["checks"]["db"]
    assert check["status"] == "error"
    assert "pool not initialised" in check["error"]


def test_ready_db_ok_when_select_1_succeeds(monkeypatch) -> None:
    queries: list[str] = []

    class Conn:
        async def fetchval(self, query: str) -> int:
            queries.append(query)
            return 1

    class Pool:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield Conn()

    monkeypatch.setenv("REGISTRATION_DB_DSN", "postgres://unused")
    monkeypatch.setattr(db, "_pool", Pool())
    resp = _ready()
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"]["db"] == {"status": "ok"}
    assert queries == ["SELECT 1"]
