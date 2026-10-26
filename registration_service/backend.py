"""Agent process backend — spawn, stop, and health-check agent processes.

Three backends share one interface (ContainerBackend):

  * SubprocessBackend — local dev. Each agent is an asyncio subprocess running
    registration_service.runner. Custom agents with requirements get a per-agent
    virtualenv (created with --system-site-packages so the framework packages
    remain importable) into which their requirements are installed.

  * ClusterBackend — ENGR cluster. Each agent is launched as an `srun --overlap`
    step inside the existing Slurm allocation, using its own per-agent Docker
    image. Only uploaded-code agents (``custom``, or ``llm`` with uploaded tools)
    run here; the supervisor rejects the others on this backend.

  * CloudRunBackend — GCP. Each custom agent is deployed as a scale-to-zero Cloud
    Run service off the CI-built image (Cloud Run Admin API), run as a shared
    low-privilege SA, with the chat app's SA granted run.invoker. Shares Cluster's
    out-of-band build; only the launch mechanism differs (API call, no host loop).

make_backend() selects ClusterBackend when SPAWN_BACKEND == "cluster",
CloudRunBackend when SPAWN_BACKEND == "cloudrun", else SubprocessBackend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agentnet.settings import DATABASE_URL, OLD_DATABASE_NAMES, PG_ADDRESS_NAMES
from registration_service.archive import IGNORED_DIRS
from registration_service.models import AnyConfig, UploadSpec, normalize_handler_type, upload_spec

logger = logging.getLogger(__name__)

_DEFAULT_HEALTH_TIMEOUT_SECONDS = 30.0
_DEFAULT_INSTALL_TIMEOUT_SECONDS = 600.0
_HEALTH_POLL_INTERVAL_SECONDS = 0.5
# SIGTERM -> SIGKILL grace when stopping an agent's process group.
_STOP_GRACE_SECONDS = 5.0
# A spawned agent is ready once its A2A app serves the agent card. "Anything
# below 500 on /health" was not enough: the A2A app has no /health, so any
# listener on the port (an orphan, another program) passed with a 404.
_READY_PATH = "/.well-known/agent-card.json"
# A spawned agent, and each step that installs its code, gets the service's
# environment minus the database address (the URL under either name, and the PG*
# variables the service may build it from): agents never need the platform's
# database, and many frameworks and Postgres clients connect to whatever these name.
# (deployment/spawn_agent_loop.py does the same on the cluster.)
_AGENT_ENV_EXCLUDED = (DATABASE_URL, *OLD_DATABASE_NAMES, *PG_ADDRESS_NAMES)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_SKELETON_DIR = _REPO_ROOT / "agent_skeleton"

# Whether an agent's code is uploaded (and so needs HANDLER_FILE_PATH, the project
# root on PYTHONPATH, and a per-agent venv for its requirements) is a property of
# the CONFIG, not of the handler_type name — ``upload_spec(config)`` answers it for
# both uploaded-code shapes (``custom`` and ``llm`` + ``tools.source='upload'``).


def _env_without_database() -> dict[str, str]:
    """The service's environment minus ``_AGENT_ENV_EXCLUDED``: what an agent and its
    install steps start from."""
    return {k: v for k, v in os.environ.items() if k not in _AGENT_ENV_EXCLUDED}


def _handler_files_dir() -> Path:
    return Path(os.getenv("HANDLER_FILES_DIR", "./uploaded_handlers"))


def _agent_log_dir() -> Path:
    return Path(os.getenv("AGENT_LOG_DIR", "logs/agents"))


def _health_timeout_seconds() -> float:
    return float(os.getenv("AGENT_SPAWN_HEALTH_TIMEOUT_SECONDS", str(_DEFAULT_HEALTH_TIMEOUT_SECONDS)))


def _signal_group(pid: int, sig: int) -> None:
    """Signal an agent's whole process group (it leads its own session)."""
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # already gone (macOS reports EPERM for a zombie leader)


def _has_build_metadata(handler_dir: Path) -> bool:
    """True if the uploaded project root ships pip-installable build metadata."""
    return (handler_dir / "pyproject.toml").exists() or (handler_dir / "setup.py").exists()


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
    """The nested AGENT_CONFIG_JSON envelope shared by every handler type (model input).

    ``config`` is already typed, so its dump is in the CANONICAL shape. Send the
    canonical name with it: a row stored under a legacy name (``tool_loop``,
    ``endpoint_ops``) would otherwise make the runner read this canonical payload
    with the legacy-shape reader, which drops ``extra`` or rejects the config.
    """
    config_payload = config.model_dump()
    config_payload["name"] = name
    config_payload["description"] = description
    return _envelope_json(agent_id, normalize_handler_type(handler_type), port, config_payload)


def cluster_srun_argv(
    *,
    agent_id: str,
    name: str,
    description: str,
    handler_type: str,
    config: dict,
    port: int,
    build_commit: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build the host-side `srun --overlap` argv + per-agent env for an uploaded-code agent.

    Used by the registration service's /internal/spawn-queue so the host spawn loop
    (deployment/spawn_agent_loop.py) can launch the agent in the native Slurm env.
    Only custom agents reach this path (image_ready comes from the custom build flow).
    AGENT_CONFIG_JSON is passed via env (not --export) to avoid comma/quote breakage;
    --export=ALL then propagates it (and the host's OPENAI_* etc.) into the container.

    The image is the one tagged with the approval's commit (``build_commit``), so
    the agent runs the code that was approved even if ':latest' has moved on. A
    tag, not the digest: pyxis reads an '@' in the image as a user separator.
    A row without a commit (built before it was recorded) runs ':latest'.
    """
    image = (
        f"{_image_registry()}/agenticnetwork-custom-{_image_slug(name)}"
        f":{build_commit or 'latest'}"
    )
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
        # Use the image's own interpreter by ABSOLUTE path and drop any leaked
        # PYTHONPATH. --export=ALL propagates the submit shell's conda env, so a
        # bare `python` resolves to the host anaconda (e.g. 3.7) instead of the
        # image's /usr/local/bin/python (3.11) — which then can't run 3.9+ syntax.
        "/bin/sh", "-c",
        "unset PYTHONPATH; exec /usr/local/bin/python -m agent_skeleton.container_runner",
    ]
    return argv, {"AGENT_CONFIG_JSON": envelope}


class ContainerBackend(ABC):
    # Capability flags — defaults match SubprocessBackend. A backend that builds
    # out-of-band (commit->CI->registry) and/or launches via an external host loop
    # (e.g. ClusterBackend, future CloudRunBackend) overrides the relevant ones.
    owns_build: bool = False
    needs_host_loop: bool = False
    supports_health_restart: bool = True
    publishes_decoupled: bool = False

    @abstractmethod
    async def spawn(
        self,
        agent_id: str,
        name: str,
        description: str,
        handler_type: str,
        config: AnyConfig,
        port: int,
    ) -> str | None:
        """Launch the agent. Returns the externally-reachable base URL for
        backends that address agents off-host (CloudRunBackend → the run.app URL);
        the local/host-loop backends bind localhost and return None."""
        ...

    @abstractmethod
    async def stop(self, agent_id: str) -> None: ...

    @abstractmethod
    async def is_alive(self, agent_id: str) -> bool: ...

    async def stop_all(self) -> list[str]:
        """Stop every agent whose lifetime is tied to this process (service
        shutdown) and return their ids. Default: none — cluster srun steps and
        Cloud Run services outlive the registration service."""
        return []


class SubprocessBackend(ContainerBackend):
    """Spawns each registered agent as an asyncio subprocess.

    The runner script (registration_service/runner.py) reads agent config from
    the AGENT_CONFIG_JSON environment variable and starts the appropriate handler.
    Each agent runs in its own session; its stdout+stderr go to
    ``$AGENT_LOG_DIR/<agent_id>.log`` (default ``logs/agents`` under the cwd).
    """

    def __init__(self) -> None:
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        # agent_id -> (log file, offset where the current run's output starts)
        self._logs: dict[str, tuple[Path, int]] = {}

    async def spawn(
        self,
        agent_id: str,
        name: str,
        description: str,
        handler_type: str,
        config: AnyConfig,
        port: int,
    ) -> None:
        # A second spawn for a running agent (approve twice, a restart racing the
        # health loop) would orphan the first process, which still holds the port.
        await self.stop(agent_id)

        env = _env_without_database()
        env["AGENT_CONFIG_JSON"] = _config_envelope(
            agent_id, name, description, handler_type, config, port
        )
        # The runner exits once this process is gone (see runner._exit_with_parent):
        # agents leave our process group, so a SIGKILL of the service reaches nobody.
        env["AGENT_PARENT_PID"] = str(os.getpid())

        python = sys.executable
        # The runner imports registration_service, agent_skeleton and agentnet, which
        # must resolve whatever the service's cwd is.
        pythonpath = [str(_REPO_ROOT)]

        spec = upload_spec(config)
        if spec is not None:
            handler_dir = _handler_files_dir() / agent_id
            env["HANDLER_FILE_PATH"] = str(handler_dir / spec.entry_file)
            if spec.system_packages:
                # apt is a cluster-image concern; we can't apt-install per-agent
                # in local subprocess dev, so skip and warn. If the handler needs a
                # system lib locally, install it on the host first.
                logger.warning(
                    "subprocess_skipping_system_packages agent_id=%s packages=%s "
                    "(apt only applies to the cluster image build)",
                    agent_id, spec.system_packages,
                )
            # The uploaded project root (handler_dir) also goes on the path, first,
            # so a multi-file handler can `import` its siblings. Single-file
            # handlers never needed it; it's harmless for them.
            pythonpath.insert(0, str(handler_dir))
            if spec.requirements or (spec.install_project and _has_build_metadata(handler_dir)):
                python = await self._ensure_venv(handler_dir, spec)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in [*pythonpath, env.get("PYTHONPATH", "")] if p
        )

        # Output goes to a per-agent file: PIPEs that nobody drains block the
        # agent once the buffer fills. Appending keeps earlier runs; the offset
        # marks where this run starts, for the crash report.
        log_path = _agent_log_dir() / f"{agent_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        runner_path = os.path.join(os.path.dirname(__file__), "runner.py")
        with open(log_path, "ab") as log:
            started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            log.write(f"--- spawn agent_id={agent_id} port={port} at {started} ---\n".encode())
            log.flush()
            offset = log.tell()
            proc = await asyncio.create_subprocess_exec(
                python,
                runner_path,
                env=env,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                # Own session and process group: stop() signals the whole group,
                # and a Ctrl-C aimed at the service doesn't hit the agents before
                # the service's shutdown stops them.
                start_new_session=True,
            )
        self._procs[agent_id] = proc
        self._logs[agent_id] = (log_path, offset)
        logger.info(
            "subprocess_spawned agent_id=%s pid=%d port=%d handler_type=%s log=%s",
            agent_id, proc.pid, port, handler_type, log_path,
        )

        try:
            await self._wait_for_health(agent_id, port)
        except BaseException:
            # A spawn reported as failed must not leave its process holding the
            # port (and possibly coming up later, unpublished).
            await self.stop(agent_id)
            raise

    async def _ensure_venv(self, handler_dir: Path, spec: UploadSpec) -> str:
        """Create a per-agent venv (if absent) and install project + requirements.

        Order mirrors the cluster Dockerfile (see builder.py): framework, then the
        uploaded project (`pip install .` if it ships build metadata and
        install_project is set), then requirements LAST so they win.
        Returns the venv's python. system_packages (apt) can't be installed
        per-agent locally — the caller logs and skips them.
        """
        venv = handler_dir / ".venv"
        py = venv / "bin" / "python"
        if not py.exists():
            handler_dir.mkdir(parents=True, exist_ok=True)
            await _run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)])
            await _run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
            # Install the framework (editable) so `import agent_skeleton` works even
            # if the system site-packages don't already carry it.
            await _run([str(py), "-m", "pip", "install", "--quiet", "-e", str(_AGENT_SKELETON_DIR)])
        if spec.install_project and _has_build_metadata(handler_dir):
            # From a copy: pip builds in the source tree (build/, *.egg-info/),
            # and the stored tree must stay the code the admin reviewed.
            with tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp) / "project"
                copy = asyncio.ensure_future(asyncio.to_thread(
                    shutil.copytree, handler_dir, project, ignore=shutil.ignore_patterns(*IGNORED_DIRS)
                ))
                try:
                    await asyncio.shield(copy)
                except asyncio.CancelledError:
                    # A cancel does not stop the copy thread: let it finish
                    # before the directory is removed, so no copy is left.
                    await asyncio.gather(copy, return_exceptions=True)
                    raise
                await _run([str(py), "-m", "pip", "install", "--quiet", str(project)])
        if spec.requirements:
            await _run([str(py), "-m", "pip", "install", "--quiet", *spec.requirements])
        return str(py)

    async def stop(self, agent_id: str) -> None:
        proc = self._procs.pop(agent_id, None)
        self._logs.pop(agent_id, None)
        if proc is None:
            return
        if proc.returncode is None:
            _signal_group(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)
            except asyncio.TimeoutError:
                _signal_group(proc.pid, signal.SIGKILL)
                await proc.wait()
        logger.info("subprocess_stopped agent_id=%s pid=%d code=%s", agent_id, proc.pid, proc.returncode)

    async def stop_all(self) -> list[str]:
        agent_ids = list(self._procs)
        await asyncio.gather(*(self.stop(a) for a in agent_ids), return_exceptions=True)
        return agent_ids

    async def is_alive(self, agent_id: str) -> bool:
        proc = self._procs.get(agent_id)
        if proc is None:
            return False
        return proc.returncode is None

    def _log_tail(self, agent_id: str, limit: int = 2000) -> str:
        """This run's output so far (the end of it), for error messages."""
        path, offset = self._logs.get(agent_id, (None, 0))
        if path is None:
            return ""
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError:
            return ""
        return data.decode(errors="replace")[-limit:]

    async def _wait_for_health(self, agent_id: str, port: int) -> None:
        url = f"http://127.0.0.1:{port}{_READY_PATH}"
        timeout = _health_timeout_seconds()
        log_path = self._logs.get(agent_id, (None, 0))[0]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with httpx.AsyncClient() as client:
            while loop.time() < deadline:
                proc = self._procs.get(agent_id)
                if proc is None:
                    raise RuntimeError(f"Agent {agent_id} was stopped while starting")
                if proc.returncode is not None:
                    output_tail = self._log_tail(agent_id)
                    logger.error(
                        "subprocess_crashed agent_id=%s code=%d log=%s output=%s",
                        agent_id, proc.returncode, log_path, output_tail,
                    )
                    raise RuntimeError(
                        f"Agent process exited with code {proc.returncode} before becoming healthy "
                        f"(log: {log_path}). output: {output_tail[-500:]}"
                    )
                try:
                    resp = await client.get(url, timeout=2.0)
                    if resp.status_code == 200:
                        logger.info("subprocess_healthy agent_id=%s port=%d", agent_id, port)
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(_HEALTH_POLL_INTERVAL_SECONDS)
        output_tail = self._log_tail(agent_id)
        logger.error(
            "subprocess_health_timeout agent_id=%s port=%d log=%s output=%s",
            agent_id, port, log_path, output_tail,
        )
        raise RuntimeError(
            f"Agent {agent_id} did not serve {_READY_PATH} within {timeout:g}s on port {port} "
            f"(log: {log_path}). output: {output_tail[-500:]}"
        )


class ClusterBackend(ContainerBackend):
    """ENGR cluster spawning via a host-side loop (plan Note #8).

    `srun` is NOT available inside the registration container (verified on the
    cluster: the image has no Slurm client tools), so this backend does not spawn
    directly. Instead, the build-poll marks built agents `image_ready`; the host
    loop (deployment/spawn_agent_loop.py) — running in submit.sbatch's native Slurm
    env where `srun` works — pulls /internal/spawn-queue, runs `srun --overlap`,
    and reports the PID back via /internal/spawned (which calls record_pid).

    So here: spawn() is a no-op; is_alive() fetches the agent's card; stop()
    queues the PID for the host loop to kill via /internal/stop-queue.
    """

    owns_build = True
    needs_host_loop = True
    supports_health_restart = False

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
        if pid <= 1:
            logger.warning("cluster_pid_ignored agent_id=%s pid=%d (not a single process)", agent_id, pid)
            return
        self._procs[agent_id] = (pid, port)
        logger.info("cluster_pid_recorded agent_id=%s pid=%d port=%d", agent_id, pid, port)

    def has_process(self, agent_id: str) -> bool:
        """True from a reported launch until the agent is stopped."""
        return agent_id in self._procs

    def discard_pid(self, pid: int) -> None:
        """Queue a reported process that is not the agent's recorded one for the
        host loop to kill, leaving the recorded one as it is."""
        if pid <= 1:
            logger.warning("cluster_pid_ignored pid=%d (not a single process)", pid)
            return
        self._stop_pids.append(pid)
        logger.info("cluster_stop_queued pid=%d (launch not taken)", pid)

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
        # Same readiness rule as SubprocessBackend: the agent's A2A app serves its
        # card (container_runner has no /health, so a 404 there proves nothing).
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"http://localhost:{port}{_READY_PATH}")
            return resp.status_code == 200
        except Exception:
            return False


def _install_timeout_seconds() -> float:
    return float(os.getenv("AGENT_INSTALL_TIMEOUT_SECONDS", str(_DEFAULT_INSTALL_TIMEOUT_SECONDS)))


async def _run(cmd: list[str]) -> None:
    """Run one install step (venv, pip). A spawn holds the agent's lock, so the
    step is bounded by AGENT_INSTALL_TIMEOUT_SECONDS: on timeout it fails the
    spawn, and on timeout or cancellation its process group is killed.

    The output goes to a temporary file, not a pipe, so the wait ends when the
    step exits, even if something it started outside its group still holds
    the output open.

    A step runs the agent's own build code (its setup.py or build backend, an
    sdist's), so it gets the same environment as the agent: no database address."""
    with tempfile.TemporaryFile() as out:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=out,
            stderr=asyncio.subprocess.STDOUT,
            env=_env_without_database(),
            # Own process group, so the kill below also reaches what pip started
            # (a build backend, git); no controlling terminal to prompt on.
            start_new_session=True,
        )
        timeout = _install_timeout_seconds()
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"command timed out after {timeout:g}s: {' '.join(cmd)}") from None
        finally:
            if proc.returncode is None:
                _signal_group(proc.pid, signal.SIGKILL)
                await proc.wait()
        if proc.returncode != 0:
            out.seek(max(0, out.seek(0, os.SEEK_END) - 1500))
            tail = out.read().decode(errors="replace")
            raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{tail}")


def _image_slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def _image_registry() -> str:
    """The registry namespace custom-agent images are built/pushed/pulled under.

    Group-owned rather than tied to a personal account; override via IMAGE_REGISTRY.
    See docs/MULTI_CONTRIBUTOR.md.
    """
    return os.environ.get("IMAGE_REGISTRY", "di2washu")


# ── Cloud Run backend ────────────────────────────────────────────────────────────

# Cloud Run always injects PORT (default 8080) and expects the container to listen
# on it. container_runner binds the AGENT_CONFIG_JSON `port` (falling back to
# AGENT_PORT), NOT $PORT — so we pin both the envelope port and the service's
# container port to this constant. The DB-allocated port is irrelevant off-host.
_CLOUD_RUN_CONTAINER_PORT = 8080
_CLOUD_RUN_DEFAULT_CPU = "1"
_CLOUD_RUN_DEFAULT_MEMORY = "512Mi"
_CLOUD_RUN_DEFAULT_MAX_INSTANCES = 1
# Registration-service env forwarded into every deployed agent (LLM wiring + ADS
# address). The agent runs auth-unaware; this mirrors what the cluster's
# --export=ALL propagates. Extend via CLOUDRUN_FORWARD_ENV (comma-separated).
_CLOUD_RUN_FORWARD_ENV_DEFAULT = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "AGENT_MODEL",
    "ADS_SERVER_ADDR",
)


def _cloudrun_service_id(name: str) -> str:
    """Cloud Run service id for an agent: `agent-<slug>`, RFC1035-safe.

    Service ids must be lowercase, start with a letter, contain only letters,
    digits and hyphens, and be <= 63 chars. `_image_slug` only lowercases +
    space->hyphen, so sanitize anything else here.
    """
    raw = _image_slug(name)
    cleaned = "".join(c if (c.isalnum() or c == "-") else "-" for c in raw)
    cleaned = "-".join(part for part in cleaned.split("-") if part)  # collapse runs
    svc = f"agent-{cleaned}".strip("-")
    return svc[:63].rstrip("-")


def _sa_member(sa_email: str | None) -> str | None:
    """IAM member string for a service-account email (idempotent if already one)."""
    if not sa_email:
        return None
    return sa_email if sa_email.startswith("serviceAccount:") else f"serviceAccount:{sa_email}"


def _cloudrun_operation_timeout() -> float:
    """Seconds to wait for a Cloud Run deploy or delete before it counts as
    failed. A deploy or stop holds the agent's lock, so it must end."""
    return float(os.getenv("CLOUDRUN_OPERATION_TIMEOUT_SECONDS", "900"))


def _forward_env_keys() -> tuple[str, ...]:
    extra = os.getenv("CLOUDRUN_FORWARD_ENV", "")
    extras = tuple(k.strip() for k in extra.split(",") if k.strip())
    return _CLOUD_RUN_FORWARD_ENV_DEFAULT + extras


def _deploy_spec(
    *,
    agent_id: str,
    name: str,
    description: str,
    handler_type: str,
    config: AnyConfig,
    image_ref: str | None,
    service_account: str | None,
    invoker_member: str | None,
    vpc_connector: str | None,
    resource: dict | None = None,
    build_commit: str | None = None,
) -> dict:
    """Pure description of the Cloud Run service to deploy (no API calls).

    Image is composed here per the Step-3 contract: `image_ref` is a *bare* digest
    (`sha256:...`) of the image tagged with the approval's commit, so deploy
    `name@digest`. When it's None (digest lookup is best-effort), deploy that
    commit's tag, and `:latest` for a row with no recorded commit (older rows
    predate both columns).
    """
    image_name = f"{_image_registry()}/agenticnetwork-custom-{_image_slug(name)}"
    if image_ref:
        image = f"{image_name}@{image_ref}"
    else:
        image = f"{image_name}:{build_commit or 'latest'}"

    env = {
        k: os.environ[k] for k in _forward_env_keys() if os.environ.get(k) is not None
    }
    # Pin the port the container binds (see _CLOUD_RUN_CONTAINER_PORT note).
    env["AGENT_CONFIG_JSON"] = _config_envelope(
        agent_id, name, description, handler_type, config, _CLOUD_RUN_CONTAINER_PORT
    )
    env["AGENT_PORT"] = str(_CLOUD_RUN_CONTAINER_PORT)

    resource = resource or {}
    # concurrency is None-safe: omitted (None) → Cloud Run's default is left in
    # place; set → pinned (e.g. 1 for per-request isolation of untrusted agents).
    concurrency = resource.get("concurrency")
    return {
        "service_id": _cloudrun_service_id(name),
        "image": image,
        "container_port": _CLOUD_RUN_CONTAINER_PORT,
        "min_instances": 0,
        "max_instances": int(resource.get("max_instances", _CLOUD_RUN_DEFAULT_MAX_INSTANCES)),
        "cpu": str(resource.get("cpu", _CLOUD_RUN_DEFAULT_CPU)),
        "memory": str(resource.get("memory", _CLOUD_RUN_DEFAULT_MEMORY)),
        "concurrency": int(concurrency) if concurrency is not None else None,
        "service_account": service_account,
        "ingress": "internal-only",
        "allow_unauthenticated": False,
        "vpc_connector": vpc_connector or None,
        "env": env,
        "invoker_member": invoker_member,
    }


class _CloudRunClient:
    """Thin wrapper over the Cloud Run Admin API (`google-cloud-run`).

    The ONLY place the google client library is imported, and it's imported
    lazily on construction so `backend.py` (and `make_backend`) import fine on
    hosts without the package — unit tests inject a fake in its place.
    """

    def __init__(self, project: str, region: str) -> None:
        from google.cloud import run_v2  # lazy: keep the dep out of import time

        self._run_v2 = run_v2
        self._services = run_v2.ServicesAsyncClient()
        self._project = project
        self._region = region
        self._parent = f"projects/{project}/locations/{region}"

    def _service_name(self, service_id: str) -> str:
        return f"{self._parent}/services/{service_id}"

    def _build_service(self, spec: dict):
        run_v2 = self._run_v2
        container = run_v2.Container(
            image=spec["image"],
            ports=[run_v2.ContainerPort(container_port=spec["container_port"])],
            env=[run_v2.EnvVar(name=k, value=v) for k, v in spec["env"].items()],
            resources=run_v2.ResourceRequirements(
                limits={"cpu": spec["cpu"], "memory": spec["memory"]}
            ),
        )
        template = run_v2.RevisionTemplate(
            containers=[container],
            scaling=run_v2.RevisionScaling(
                min_instance_count=spec["min_instances"],
                max_instance_count=spec["max_instances"],
            ),
        )
        if spec.get("concurrency") is not None:
            template.max_instance_request_concurrency = spec["concurrency"]
        if spec["service_account"]:
            template.service_account = spec["service_account"]
        if spec["vpc_connector"]:
            template.vpc_access = run_v2.VpcAccess(
                connector=spec["vpc_connector"],
                egress=run_v2.VpcAccess.VpcEgress.PRIVATE_RANGES_ONLY,
            )
        return run_v2.Service(
            template=template,
            ingress=run_v2.IngressTraffic.INGRESS_TRAFFIC_INTERNAL_ONLY,
        )

    async def create_or_replace(self, spec: dict) -> str:
        """Create the service (or replace it if it already exists); return its URI."""
        from google.api_core.exceptions import AlreadyExists

        service = self._build_service(spec)
        name = self._service_name(spec["service_id"])
        try:
            op = await self._services.create_service(
                parent=self._parent, service=service, service_id=spec["service_id"]
            )
        except AlreadyExists:
            service.name = name
            op = await self._services.update_service(service=service)
        result = await op.result(timeout=_cloudrun_operation_timeout())
        return result.uri

    async def delete(self, service_id: str) -> None:
        """Delete the service; one that does not exist counts as deleted."""
        from google.api_core.exceptions import NotFound

        try:
            op = await self._services.delete_service(name=self._service_name(service_id))
            await op.result(timeout=_cloudrun_operation_timeout())
        except NotFound:
            return

    async def get(self, service_id: str) -> dict | None:
        from google.api_core.exceptions import NotFound

        try:
            svc = await self._services.get_service(name=self._service_name(service_id))
        except NotFound:
            return None
        ready = any(
            c.type_ == "Ready" and c.state == self._run_v2.Condition.State.CONDITION_SUCCEEDED
            for c in svc.conditions
        )
        return {"ready": ready, "uri": svc.uri}

    async def set_invoker(self, service_id: str, member: str) -> None:
        """Grant `member` roles/run.invoker on the service (additive, idempotent)."""
        from google.iam.v1 import iam_policy_pb2, policy_pb2

        name = self._service_name(service_id)
        policy = await self._services.get_iam_policy(
            request=iam_policy_pb2.GetIamPolicyRequest(resource=name)
        )
        binding = next((b for b in policy.bindings if b.role == "roles/run.invoker"), None)
        if binding is None:
            binding = policy_pb2.Binding(role="roles/run.invoker")
            policy.bindings.append(binding)
        if member not in binding.members:
            binding.members.append(member)
            await self._services.set_iam_policy(
                request=iam_policy_pb2.SetIamPolicyRequest(resource=name, policy=policy)
            )


class CloudRunBackend(ContainerBackend):
    """Deploys each custom agent as a scale-to-zero Cloud Run service.

    Like ClusterBackend, the build is out-of-band (commit -> CI -> Docker Hub) and
    spawn() only runs once the image is ready. Unlike Cluster, there is no host
    loop: this backend calls the Cloud Run Admin API directly (needs_host_loop=
    False), so the supervisor invokes spawn() itself when an agent reaches
    image_ready. Liveness is an API get (never an HTTP GET — that would cold-start
    a min=0 service), and the card is published decoupled so it persists at zero
    instances (publishes_decoupled=True).
    """

    owns_build = True
    needs_host_loop = False
    supports_health_restart = False
    publishes_decoupled = True

    def __init__(self, client: object | None = None) -> None:
        self._project = os.getenv("GCP_PROJECT", "")
        self._region = os.getenv("GCP_REGION", "us-central1")
        self._service_account = os.getenv("AGENT_RUNTIME_SA") or None
        self._invoker_member = _sa_member(os.getenv("CHAT_APP_INVOKER_SA"))
        if self._invoker_member is None:
            # Not an error (a deploy may grant access some other way), but without it the
            # chat app gets 403 from every agent this backend deploys.
            logger.warning("cloudrun_no_invoker_sa: CHAT_APP_INVOKER_SA is unset; deployed agents grant "
                           "no run.invoker, so the chat app cannot call them")
        self._vpc_connector = os.getenv("VPC_CONNECTOR") or None
        # Lazily built on first use so make_backend()/import work without the
        # google-cloud-run package (and so unit tests can inject a fake).
        self._client = client

    def _cr_client(self) -> _CloudRunClient:
        if self._client is None:
            if not self._project:
                raise RuntimeError("CloudRunBackend requires GCP_PROJECT to be set")
            self._client = _CloudRunClient(self._project, self._region)
        return self._client  # type: ignore[return-value]

    async def spawn(
        self,
        agent_id: str,
        name: str,
        description: str,
        handler_type: str,
        config: AnyConfig,
        port: int,
    ) -> str | None:
        # image_ref (Step-3 contract), build_commit and resources (Step 8) are read
        # off the agent row here; _deploy_spec falls back to the commit tag (or
        # :latest) / default sizing when absent.
        from registration_service.db import get_agent

        row = await get_agent(agent_id)
        image_ref = (row or {}).get("image_ref")
        build_commit = (row or {}).get("build_commit")
        # asyncpg returns JSONB as a string; fake_db returns a dict. Handle both
        # (same guard used for config/oasf_skills throughout the supervisor).
        resources = (row or {}).get("resources")
        if isinstance(resources, str):
            resources = json.loads(resources)

        # Backend-selection seam: a GPU (or future strict-isolation) request means
        # Cloud Run is the wrong home — it can't schedule GPUs. Fail loudly rather
        # than deploy somewhere the agent can't run; routing these to a GKEBackend
        # is the follow-up.
        if resources and resources.get("gpu"):
            raise RuntimeError(
                f"agent {name!r} requests a GPU, which Cloud Run cannot provide; "
                "no GKEBackend is configured to host GPU/strict-isolation agents"
            )

        spec = _deploy_spec(
            agent_id=agent_id,
            name=name,
            description=description,
            handler_type=handler_type,
            config=config,
            image_ref=image_ref,
            build_commit=build_commit,
            service_account=self._service_account,
            invoker_member=self._invoker_member,
            vpc_connector=self._vpc_connector,
            resource=resources,
        )
        client = self._cr_client()
        url = await client.create_or_replace(spec)
        if self._invoker_member:
            await client.set_invoker(spec["service_id"], self._invoker_member)
        logger.info(
            "cloudrun_spawned agent_id=%s service=%s image=%s url=%s",
            agent_id, spec["service_id"], spec["image"], url,
        )
        return url

    async def stop(self, agent_id: str) -> None:
        from registration_service.db import get_agent

        row = await get_agent(agent_id)
        if row is None:
            return
        service_id = _cloudrun_service_id(row["name"])
        await self._cr_client().delete(service_id)
        logger.info("cloudrun_stopped agent_id=%s service=%s", agent_id, service_id)

    async def is_alive(self, agent_id: str) -> bool:
        # Cloud Run Admin API only — an HTTP GET would cold-start a min=0 service.
        # "Exists & Ready (even at 0 instances)" counts as alive.
        from registration_service.db import get_agent

        row = await get_agent(agent_id)
        if row is None:
            return False
        svc = await self._cr_client().get(_cloudrun_service_id(row["name"]))
        return bool(svc and svc.get("ready"))


def make_backend() -> ContainerBackend:
    backend = os.getenv("SPAWN_BACKEND")
    if backend == "cluster":
        return ClusterBackend()
    if backend == "cloudrun":
        return CloudRunBackend()
    return SubprocessBackend()
