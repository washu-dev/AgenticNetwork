"""The service as a real process, judged only through HTTP, A2A and the OS.

``python -m registration_service.server`` in the native dev-stack mode
(FAKE_DB=1 FAKE_SUPERVISOR=0, a fake dirctl, a stub LLM): it must report ready,
register an llm agent that answers message/send and message/stream, and on
SIGTERM stop that agent itself and drop its card, leaving no process and no
port behind.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time

import httpx
import pytest

pytest.importorskip("a2a")
pytest.importorskip("uvicorn")

from registration_service.backend import _REPO_ROOT  # noqa: E402
from registration_service.port_pool import port_free_on_host  # noqa: E402
from registration_service.tests.agent_stubs import (  # noqa: E402
    a2a_call,
    fake_dirctl_env,
    free_port,
    pid_alive,
    routed_cids,
    stub_openai,
)
from registration_service.tests.test_native_supervisor import LLM_AGENT  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("pgrep") is None, reason="needs pgrep")


def _children(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def _wait(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


async def test_service_process_registers_serves_and_cleans_up_on_sigterm(tmp_path) -> None:
    dirctl_env, state = fake_dirctl_env(tmp_path)
    service_port = free_port()
    base = f"http://127.0.0.1:{service_port}"
    agent_logs = tmp_path / "agent-logs"
    service_log = tmp_path / "service.log"
    agent_pids: list[int] = []

    with stub_openai() as (llm_url, _), open(service_log, "wb") as log:
        env = {
            **os.environ,
            "FAKE_DB": "1",
            "FAKE_SUPERVISOR": "0",
            "SPAWN_BACKEND": "subprocess",
            "ADS_SERVER_ADDR": "fake-dir:8888",
            **dirctl_env,
            "ADS_ROUTING_PUBLISH_POLL_SECONDS": "0.05",
            "OPENAI_BASE_URL": llm_url,
            "OPENAI_API_KEY": "test-placeholder",
            "AGENT_LOG_DIR": str(agent_logs),
            "REGISTRATION_SERVICE_PORT": str(service_port),
            "REGISTRATION_SERVICE_HOST": "127.0.0.1",
            "AGENT_BIND_HOST": "127.0.0.1",
        }
        env.pop("PYTHONPATH", None)
        service = subprocess.Popen(
            [sys.executable, "-m", "registration_service.server"],
            cwd=_REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            async with httpx.AsyncClient(base_url=base, timeout=60.0) as http:
                for _ in range(200):
                    try:
                        if (await http.get("/health")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail("service never answered /health:\n" + service_log.read_text())

                ready = await http.get("/ready")
                assert ready.status_code == 200, ready.text
                assert ready.json()["checks"]["directory"]["status"] == "ok"

                r = await http.post("/agents", json=LLM_AGENT)
                assert r.status_code == 201, r.text + service_log.read_text()
                agent = r.json()
                assert agent["status"] == "live" and agent["ads_cid"]
                assert routed_cids(state) == [agent["ads_cid"]]

            for stream in (False, True):
                result = await a2a_call(agent["endpoint_url"], "hi", stream=stream)
                assert result["text"] == "stub-llm says: hi"

            agent_pids = _children(service.pid)
            assert len(agent_pids) == 1, agent_pids

            service.send_signal(signal.SIGTERM)
            # uvicorn finishes a graceful shutdown, then re-raises the signal.
            assert service.wait(timeout=30) in (0, -signal.SIGTERM)
            assert "Application shutdown complete" in service_log.read_text()
            assert _wait(lambda: not any(pid_alive(p) for p in agent_pids), 10)
            assert port_free_on_host(agent["port"]) and port_free_on_host(service_port)
            assert routed_cids(state) == []
            # Stopped by the service's shutdown, not by its own orphan watch.
            assert "runner_parent_exited" not in (agent_logs / f"{agent['id']}.log").read_text()
        finally:
            if service.poll() is None:
                service.kill()
                service.wait()
            for pid in agent_pids:
                if pid_alive(pid):
                    os.killpg(pid, signal.SIGKILL)
