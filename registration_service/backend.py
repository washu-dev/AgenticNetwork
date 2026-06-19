"""Agent process backend — spawn, stop, and health-check agent subprocesses."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from abc import ABC, abstractmethod

import httpx

from registration_service.models import LLMConfig, APIProxyConfig

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT_SECONDS = 30.0
_HEALTH_POLL_INTERVAL_SECONDS = 0.5


class ContainerBackend(ABC):
    @abstractmethod
    async def spawn(
        self,
        agent_id: str,
        name: str,
        description: str,
        handler_type: str,
        config: LLMConfig | APIProxyConfig,
        port: int,
    ) -> None: ...

    @abstractmethod
    async def stop(self, agent_id: str) -> None: ...

    @abstractmethod
    async def is_alive(self, agent_id: str) -> bool: ...


class SubprocessBackend(ContainerBackend):
    """Spawns each registered agent as an asyncio subprocess.

    The runner script (registration_service/runner.py) reads agent config from
    the AGENT_CONFIG_JSON environment variable and starts the appropriate handler.
    """

    def __init__(self) -> None:
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    async def spawn(
        self,
        agent_id: str,
        name: str,
        description: str,
        handler_type: str,
        config: LLMConfig | APIProxyConfig,
        port: int,
    ) -> None:
        import json

        config_payload = config.model_dump()
        config_payload["name"] = name
        config_payload["description"] = description

        config_json = json.dumps(
            {
                "agent_id": agent_id,
                "handler_type": handler_type,
                "port": port,
                "config": config_payload,
            }
        )

        env = {**os.environ, "AGENT_CONFIG_JSON": config_json}
        runner_path = os.path.join(os.path.dirname(__file__), "runner.py")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            runner_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._procs[agent_id] = proc
        logger.info("subprocess_spawned agent_id=%s pid=%d port=%d", agent_id, proc.pid, port)

        await self._wait_for_health(agent_id, port)

    async def stop(self, agent_id: str) -> None:
        proc = self._procs.pop(agent_id, None)
        if proc is None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            logger.info("subprocess_stopped agent_id=%s pid=%d", agent_id, proc.pid)
        except ProcessLookupError:
            pass

    async def is_alive(self, agent_id: str) -> bool:
        proc = self._procs.get(agent_id)
        if proc is None:
            return False
        return proc.returncode is None

    async def _wait_for_health(self, agent_id: str, port: int) -> None:
        url = f"http://localhost:{port}/health"
        deadline = asyncio.get_event_loop().time() + _HEALTH_TIMEOUT_SECONDS
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                proc = self._procs.get(agent_id)
                if proc is not None and proc.returncode is not None:
                    stderr_bytes = b""
                    if proc.stderr:
                        try:
                            stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
                        except Exception:
                            pass
                    stderr_tail = stderr_bytes.decode(errors="replace")[-2000:]
                    logger.error(
                        "subprocess_crashed agent_id=%s code=%d stderr=%s",
                        agent_id, proc.returncode, stderr_tail,
                    )
                    raise RuntimeError(
                        f"Agent process exited with code {proc.returncode} before becoming healthy. "
                        f"stderr: {stderr_tail[-500:]}"
                    )
                try:
                    resp = await client.get(url, timeout=2.0)
                    if resp.status_code < 500:
                        logger.info("subprocess_healthy agent_id=%s port=%d", agent_id, port)
                        return
                except (httpx.ConnectError, httpx.TimeoutException):
                    pass
                await asyncio.sleep(_HEALTH_POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"Agent {agent_id} did not become healthy within {_HEALTH_TIMEOUT_SECONDS}s on port {port}"
        )
