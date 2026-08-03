"""Agent process backend — spawn, stop, and health-check agent processes.

Three backends share one interface (ContainerBackend):

  * SubprocessBackend — local dev. Each agent is an asyncio subprocess running
    registration_service.runner. Custom agents with requirements get a per-agent
    virtualenv (created with --system-site-packages so the framework packages
    remain importable) into which their requirements are installed.

  * ClusterBackend — ENGR cluster. Each agent is launched as an `srun --overlap`
    step inside the existing Slurm allocation, using a per-agent (custom) or
    shared (llm/api_proxy) Docker image.

  * CloudRunBackend — GCP. Each custom agent is deployed as a scale-to-zero Cloud
    Run service off the CI-built image (Cloud Run Admin API), run as a shared
    low-privilege SA, with the planner SA granted run.invoker. Shares Cluster's
    out-of-band build; only the launch mechanism differs (API call, no host loop).

make_backend() selects ClusterBackend when SPAWN_BACKEND == "cluster",
CloudRunBackend when SPAWN_BACKEND == "cloudrun", else SubprocessBackend.
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

from registration_service.models import APIProxyConfig, CustomConfig, LLMConfig, ToolLoopConfig

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT_SECONDS = 30.0
_HEALTH_POLL_INTERVAL_SECONDS = 0.5

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_SKELETON_DIR = _REPO_ROOT / "agent_skeleton"

AnyConfig = LLMConfig | APIProxyConfig | CustomConfig | ToolLoopConfig

# Handler types whose code is uploaded and run in-process (need HANDLER_FILE_PATH,
# the project root on PYTHONPATH, and a per-agent venv for their requirements).
_UPLOAD_CODE_TYPES = ("custom", "tool_loop")


def _handler_files_dir() -> Path:
    return Path(os.getenv("HANDLER_FILES_DIR", "./uploaded_handlers"))


def _has_build_metadata(handler_dir: Path) -> bool:
    """True if the uploaded project root ships pip-installable build metadata (D8)."""
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
    image = f"{_image_registry()}/agenticnetwork-custom-{_image_slug(name)}:latest"
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

        if handler_type in _UPLOAD_CODE_TYPES:
            assert isinstance(config, (CustomConfig, ToolLoopConfig))
            handler_dir = _handler_files_dir() / agent_id
            env["HANDLER_FILE_PATH"] = str(handler_dir / config.entry_file)
            if config.system_packages:
                # apt is a cluster-image concern (D10); we can't apt-install per-agent
                # in local subprocess dev, so skip and warn. If the handler needs a
                # system lib locally, install it on the host first.
                logger.warning(
                    "subprocess_skipping_system_packages agent_id=%s packages=%s "
                    "(apt only applies to the cluster image build)",
                    agent_id, config.system_packages,
                )
            # Both roots must be importable: registration_service + agent_skeleton
            # (framework) via _REPO_ROOT, and the uploaded project root (handler_dir)
            # so a multi-file handler can `import` its siblings. Single-file handlers
            # never needed the latter; it's harmless for them.
            env["PYTHONPATH"] = os.pathsep.join(
                p for p in [str(handler_dir), str(_REPO_ROOT), env.get("PYTHONPATH", "")] if p
            )
            if config.requirements or (config.install_project and _has_build_metadata(handler_dir)):
                python = await self._ensure_venv(handler_dir, config)

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

    async def _ensure_venv(self, handler_dir: Path, config: CustomConfig) -> str:
        """Create a per-agent venv (if absent) and install project + requirements.

        Order mirrors the cluster Dockerfile (§7.2/§7.3): framework, then the
        uploaded project (`pip install .` if it ships build metadata and
        install_project is set — D8), then requirements LAST so they win.
        Returns the venv's python. system_packages (apt, P2) can't be installed
        per-agent locally — it's log-and-skipped by the caller path in P2.
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
        if config.install_project and _has_build_metadata(handler_dir):
            await _run([str(py), "-m", "pip", "install", "--quiet", str(handler_dir)])
        if config.requirements:
            await _run([str(py), "-m", "pip", "install", "--quiet", *config.requirements])
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
) -> dict:
    """Pure description of the Cloud Run service to deploy (no API calls).

    Image is composed here per the Step-3 contract: `image_ref` is a *bare* digest
    (`sha256:...`), so deploy `name@digest`; fall back to `:latest` when it's None
    (digest lookup is best-effort and older rows predate the column).
    """
    image_name = f"{_image_registry()}/agenticnetwork-custom-{_image_slug(name)}"
    image = f"{image_name}@{image_ref}" if image_ref else f"{image_name}:latest"

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
        result = await op.result()
        return result.uri

    async def delete(self, service_id: str) -> None:
        op = await self._services.delete_service(name=self._service_name(service_id))
        await op.result()

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
        self._invoker_member = _sa_member(os.getenv("PLANNER_INVOKER_SA"))
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
        # image_ref (Step-3 contract) and resources (Step 8) are read off the agent
        # row here; _deploy_spec falls back to :latest / default sizing when absent.
        from registration_service.db import get_agent

        row = await get_agent(agent_id)
        image_ref = (row or {}).get("image_ref")
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
