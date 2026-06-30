"""Agent process backend — spawn, stop, and health-check agent processes.

Two backends share one interface (ContainerBackend):

  * SubprocessBackend — local dev. Each agent is an asyncio subprocess running
    registration_service.runner. Custom agents with requirements get a per-agent
    virtualenv (created with --system-site-packages so the framework packages
    remain importable) into which their requirements are installed.

  * ClusterBackend — ENGR cluster. Each agent is launched as an `srun --overlap`
    step inside the existing Slurm allocation, using a per-agent (custom) or
    shared (llm/api_proxy) Docker image.

make_backend() selects ClusterBackend when SPAWN_BACKEND == "cluster".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from registration_service.models import APIProxyConfig, CustomConfig, LLMConfig

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT_SECONDS = 30.0
_HEALTH_POLL_INTERVAL_SECONDS = 0.5

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_SKELETON_DIR = _REPO_ROOT / "agent_skeleton"

AnyConfig = LLMConfig | APIProxyConfig | CustomConfig


def _handler_files_dir() -> Path:
    return Path(os.getenv("HANDLER_FILES_DIR", "./uploaded_handlers"))


def _envelope_json(agent_id: str, handler_type: str, port: int, config_payload: dict) -> str:
    """The nested AGENT_CONFIG_JSON envelope (config_payload already has name/description)."""
    return json.dumps(
        {
            "agent_id": agent_id,
            "handler_type": handler_type,
            "port": port,
            "config": config_payload,
        }
    )


def _config_envelope(
    agent_id: str,
    name: str,
    description: str,
    handler_type: str,
    config: AnyConfig,
    port: int,
) -> str:
    """The nested AGENT_CONFIG_JSON envelope shared by every handler type (model input)."""
    config_payload = config.model_dump()
    config_payload["name"] = name
    config_payload["description"] = description
    return _envelope_json(agent_id, handler_type, port, config_payload)


def cluster_srun_argv(
    *,
    agent_id: str,
    name: str,
    description: str,
    handler_type: str,
    config: dict,
    port: int,
) -> tuple[list[str], dict[str, str]]:
    """Build the host-side `srun --overlap` argv + per-agent env for a custom agent.

    Used by the registration service's /internal/spawn-queue so the host spawn loop
    (deployment/spawn_agent_loop.py) can launch the agent in the native Slurm env.
    Only custom agents reach this path (image_ready comes from the custom build flow).
    AGENT_CONFIG_JSON is passed via env (not --export) to avoid comma/quote breakage;
    --export=ALL then propagates it (and the host's OPENAI_* etc.) into the container.
    """
    image = f"amaankhan25/agenticnetwork-custom-{_image_slug(name)}:latest"
    mounts = os.environ.get(
        "CLUSTER_WORKSPACE_MOUNT", "/engrfs/project/di2/AgenticNetwork:/workspace"
    )
    payload = dict(config)
    payload["name"] = name
    payload["description"] = description
    envelope = _envelope_json(agent_id, handler_type, port, payload)
    argv = [
        "srun", "--ntasks=1", "--overlap",
        f"--container-image={image}",
        f"--container-mounts={mounts}",
        "--container-writable",
        "--export=ALL",
        "python", "-m", "agent_skeleton.container_runner",
    ]
    return argv, {"AGENT_CONFIG_JSON": envelope}


class ContainerBackend(ABC):
    @abstractmethod
    async def spawn(
        self,
        agent_id: str,
        name: str,
        description: str,
        handler_type: str,
        config: AnyConfig,
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
        config: AnyConfig,
        port: int,
    ) -> None:
        env = {**os.environ}
        env["AGENT_CONFIG_JSON"] = _config_envelope(
            agent_id, name, description, handler_type, config, port
        )

        python = sys.executable

        if handler_type == "custom":
            assert isinstance(config, CustomConfig)
            handler_dir = _handler_files_dir() / agent_id
            env["HANDLER_FILE_PATH"] = str(handler_dir / "handler.py")
            # Ensure registration_service + agent_skeleton are importable from
            # the (possibly isolated) child interpreter.
            env["PYTHONPATH"] = os.pathsep.join(
                p for p in [str(_REPO_ROOT), env.get("PYTHONPATH", "")] if p
            )
            if config.requirements:
                python = await self._ensure_venv(handler_dir, config.requirements)

        runner_path = os.path.join(os.path.dirname(__file__), "runner.py")
        proc = await asyncio.create_subprocess_exec(
            python,
            runner_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._procs[agent_id] = proc
        logger.info(
            "subprocess_spawned agent_id=%s pid=%d port=%d handler_type=%s",
            agent_id, proc.pid, port, handler_type,
        )

        await self._wait_for_health(agent_id, port)

    async def _ensure_venv(self, handler_dir: Path, requirements: list[str]) -> str:
        """Create a per-agent venv (if absent) and install requirements. Returns its python."""
        venv = handler_dir / ".venv"
        py = venv / "bin" / "python"
        if not py.exists():
            handler_dir.mkdir(parents=True, exist_ok=True)
            await _run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)])
            await _run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
            # Install the framework (editable) so `import agent_skeleton` works even
            # if the system site-packages don't already carry it.
            await _run([str(py), "-m", "pip", "install", "--quiet", "-e", str(_AGENT_SKELETON_DIR)])
        if requirements:
            await _run([str(py), "-m", "pip", "install", "--quiet", *requirements])
        return str(py)

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


class ClusterBackend(ContainerBackend):
    """ENGR cluster spawning via a host-side loop (plan Note #8).

    `srun` is NOT available inside the registration container (verified on the
    cluster: the image has no Slurm client tools), so this backend does not spawn
    directly. Instead, the build-poll marks built agents `image_ready`; the host
    loop (deployment/spawn_agent_loop.py) — running in submit.sbatch's native Slurm
    env where `srun` works — pulls /internal/spawn-queue, runs `srun --overlap`,
    and reports the PID back via /internal/spawned (which calls record_pid).

    So here: spawn() is a no-op; is_alive() polls the agent's /health; stop()
    queues the PID for the host loop to kill via /internal/stop-queue.
    """

    def __init__(self) -> None:
        # agent_id -> (pid, port), reported by the host loop after a successful srun.
        self._procs: dict[str, tuple[int, int]] = {}
        # PIDs queued for the host loop to kill (consumed by /internal/stop-queue).
        self._stop_pids: list[int] = []

    async def spawn(
        self,
        agent_id: str,
        name: str,
        description: str,
        handler_type: str,
        config: AnyConfig,
        port: int,
    ) -> None:
        # No-op: the host spawn loop performs the actual srun. See class docstring.
        logger.info("cluster_spawn_noop agent_id=%s port=%d (host loop spawns)", agent_id, port)

    def record_pid(self, agent_id: str, pid: int, port: int) -> None:
        self._procs[agent_id] = (pid, port)
        logger.info("cluster_pid_recorded agent_id=%s pid=%d port=%d", agent_id, pid, port)

    def pending_stops(self) -> list[int]:
        out = self._stop_pids[:]
        self._stop_pids.clear()
        return out

    async def stop(self, agent_id: str) -> None:
        rec = self._procs.pop(agent_id, None)
        if rec:
            self._stop_pids.append(rec[0])
            logger.info("cluster_stop_queued agent_id=%s pid=%d", agent_id, rec[0])

    async def is_alive(self, agent_id: str) -> bool:
        rec = self._procs.get(agent_id)
        if rec is None:
            return False
        _pid, port = rec
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"http://localhost:{port}/health")
            return resp.status_code < 500
        except Exception:
            return False


async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        tail = (out or b"").decode(errors="replace")[-1500:]
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{tail}")


def _image_slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def make_backend() -> ContainerBackend:
    if os.getenv("SPAWN_BACKEND") == "cluster":
        return ClusterBackend()
    return SubprocessBackend()
