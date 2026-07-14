"""Process supervisor — spawn, publish, monitor, and recover registered agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ads_utils.converter import oasf_skill_tag
from ads_utils.publisher import DirectoryPublisher, DirectoryUnavailable
from a2a.types import AgentCard, AgentCapabilities, AgentSkill

from registration_service.card_credentials import build_credentials_extension

from registration_service.archive import list_tree
from registration_service.backend import (
    _AGENT_SKELETON_DIR,
    ClusterBackend,
    ContainerBackend,
    _image_registry,
    _image_slug,
    cluster_srun_argv,
    make_backend,
)
from registration_service.db import get_pool, list_agents_by_status
from registration_service.models import (
    AnyConfig,
    APIProxyConfig,
    CustomConfig,
    EndpointOpsConfig,
    LLMConfig,
)
from registration_service.port_pool import release_port

logger = logging.getLogger(__name__)

_HEALTH_POLL_SECONDS = 30.0
_BUILD_POLL_SECONDS = 30.0
_BACKOFF_BASE_SECONDS = 5.0
_MAX_RESTART_ATTEMPTS = 3


def _cluster_build_enabled() -> bool:
    """True when custom approvals should go through the GitHub → Docker Hub build."""
    return (
        os.getenv("SPAWN_BACKEND") == "cluster"
        and bool(os.getenv("GITHUB_TOKEN"))
        and bool(os.getenv("GITHUB_REPO"))
    )


class Supervisor:
    def __init__(
        self,
        backend: ContainerBackend | None = None,
        publisher: DirectoryPublisher | None = None,
    ) -> None:
        self._backend = backend or make_backend()
        self._publisher = publisher or DirectoryPublisher()
        self._health_task: asyncio.Task[None] | None = None
        self._build_task: asyncio.Task[None] | None = None

    # ── Public API ─────────────────────────────────────────────────────────────

    async def spawn_and_publish(self, agent_id: str) -> None:
        """Spawn the agent subprocess, wait for health, then publish to ADS.

        Updates agent status to 'live' on success or 'failed' on error.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agents WHERE id = $1", uuid.UUID(agent_id)
            )
        if row is None:
            raise ValueError(f"Agent {agent_id} not found in DB")

        agent = dict(row)
        config = _deserialize_config(agent)
        port: int = agent["port"]
        name: str = agent["name"]
        description: str = agent["description"]
        handler_type: str = agent["handler_type"]
        raw_skills = agent["oasf_skills"]
        oasf_skills: list = raw_skills if isinstance(raw_skills, list) else json.loads(raw_skills)

        # Custom agents must be explicitly approved before they can spawn.
        if handler_type == "custom" and not agent.get("approved", False):
            raise RuntimeError(f"Agent {agent_id} is not approved")

        # Cluster build pipeline: on first approval, commit the build context to
        # agent-directory and wait (status='building') for the image to appear on
        # Docker Hub. The build-poll loop re-invokes spawn_and_publish once the
        # image exists, at which point status is already 'building' and we fall
        # through to the actual spawn below.
        if (
            handler_type == "custom"
            and _cluster_build_enabled()
            and agent.get("status") == "registered"
        ):
            # First approval: commit the build context and wait for the image. The
            # build-poll loop flips status to 'image_ready'; the host spawn loop then
            # launches it and calls finalize_spawned (see Note #8).
            await self._commit_to_github(agent, oasf_skills)
            await _set_status(agent_id, "building")
            logger.info("custom_build_started agent_id=%s name=%s", agent_id, name)
            return

        # The cluster backend only has a spawn path for 'custom' agents (per-agent
        # image built via the GitHub→Docker Hub flow). 'llm'/'api_proxy' have no
        # cluster spawn path: ClusterBackend.spawn() is a no-op and the host srun
        # loop only launches 'image_ready' (custom) agents. Without this guard such
        # an agent would be published to ADS and marked 'live' with no process
        # behind it (a ghost-live row the planner would then dispatch to). Fail
        # loudly instead of creating that phantom.
        if isinstance(self._backend, ClusterBackend) and handler_type != "custom":
            await _set_status(agent_id, "failed")
            raise RuntimeError(
                f"handler_type={handler_type!r} is not supported on the cluster "
                "backend — only 'custom' agents have a cluster spawn path. "
                "Register it with the local subprocess backend, or use a 'custom' "
                "handler."
            )

        try:
            await self._backend.spawn(
                agent_id=agent_id,
                name=name,
                description=description,
                handler_type=handler_type,
                config=config,
                port=port,
            )
        except Exception as exc:
            logger.error("spawn_failed agent_id=%s error=%s", agent_id, exc)
            await _set_status(agent_id, "failed")
            raise

        await self._publish_and_set_live(
            agent_id, name, description, oasf_skills, port, _declared_credentials(config)
        )

    async def _publish_and_set_live(
        self,
        agent_id: str,
        name: str,
        description: str,
        oasf_skills: list,
        port: int,
        credentials: list[dict[str, Any]] | None = None,
    ) -> None:
        """Publish the agent card to ADS and mark it live. Shared by the local-spawn
        path (spawn_and_publish) and the cluster host-loop path (finalize_spawned)."""
        endpoint_url = f"http://localhost:{port}/"
        card = _build_agent_card(agent_id, name, description, oasf_skills, port, credentials)

        ads_cid: str | None = None
        try:
            record = self._publisher.publish(card, advertise_url=endpoint_url)
            ads_cid = record.backend_ref
            logger.info("ads_published agent_id=%s cid=%s", agent_id, ads_cid)
        except DirectoryUnavailable as exc:
            logger.warning("ads_publish_skipped agent_id=%s reason=%s", agent_id, str(exc).replace("\n", " | "))

        await _set_status(agent_id, "live", endpoint_url=endpoint_url, ads_cid=ads_cid)

    async def stop_and_unpublish(self, agent_id: str) -> None:
        """Unpublish from ADS, stop the subprocess, release the port."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ads_cid, port FROM agents WHERE id = $1", uuid.UUID(agent_id)
            )
        if row is None:
            return

        ads_cid: str | None = row["ads_cid"]
        port: int | None = row["port"]

        if ads_cid:
            try:
                self._publisher.unpublish(ads_cid)
            except DirectoryUnavailable as exc:
                logger.warning("ads_unpublish_failed agent_id=%s error=%s", agent_id, exc)

        await self._backend.stop(agent_id)

        async with pool.acquire() as conn:
            if port:
                await release_port(conn, port)
            await conn.execute(
                "UPDATE agents SET status = 'stopped', ads_cid = NULL, updated_at = now() "
                "WHERE id = $1",
                uuid.UUID(agent_id),
            )
        logger.info("agent_stopped agent_id=%s", agent_id)

    async def restart_with_backoff(self, agent_id: str) -> None:
        """Attempt up to _MAX_RESTART_ATTEMPTS restarts with exponential backoff.

        Sets status='failed' if all attempts are exhausted.
        """
        for attempt in range(1, _MAX_RESTART_ATTEMPTS + 1):
            delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.info(
                "restart_attempt agent_id=%s attempt=%d/%d delay=%.1fs",
                agent_id,
                attempt,
                _MAX_RESTART_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)
            await self._backend.stop(agent_id)
            try:
                await self.spawn_and_publish(agent_id)
                logger.info("restart_succeeded agent_id=%s attempt=%d", agent_id, attempt)
                return
            except Exception as exc:
                logger.warning(
                    "restart_failed agent_id=%s attempt=%d error=%s", agent_id, attempt, exc
                )

        logger.error("restart_exhausted agent_id=%s", agent_id)
        await _set_status(agent_id, "failed")

    async def recover_live_agents(self) -> None:
        """On startup, reconcile agents that were 'live' before the last shutdown.

        Local (subprocess) backend: re-spawn them in-process.

        Cluster backend: the actual agent processes were independent srun steps
        that died with the previous Slurm job, and ADS's routing index (`dir`) is
        wiped fresh each deploy — so a persisted status='live' row is a *ghost*
        (no process, not routed, invisible on the planner dashboard). The
        registration container can't re-spawn srun steps itself, but it can
        reconcile the DB to reality: re-queue custom agents whose image still
        exists on Docker Hub back to 'image_ready' (the host spawn loop then
        redeploys + republishes them), and mark the rest 'stopped' so the DB no
        longer falsely claims they're live.
        """
        pool = await get_pool()

        if not isinstance(self._backend, ClusterBackend):
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT id FROM agents WHERE status = 'live'")
            agent_ids = [str(row["id"]) for row in rows]
            if not agent_ids:
                return
            logger.info("recovery_starting count=%d", len(agent_ids))
            results = await asyncio.gather(
                *[self.spawn_and_publish(aid) for aid in agent_ids],
                return_exceptions=True,
            )
            for aid, result in zip(agent_ids, results):
                if isinstance(result, Exception):
                    logger.error("recovery_failed agent_id=%s error=%s", aid, result)
            return

        # ── Cluster backend: reconcile ghost 'live' rows to reality ──
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, handler_type, port, updated_at "
                "FROM agents WHERE status = 'live'"
            )
        if not rows:
            logger.info("recovery_skipped_cluster no_live_agents")
            return

        requeued = 0
        stopped = 0
        for row in rows:
            agent_id = str(row["id"])
            redeployable = (
                row["handler_type"] == "custom"
                and _cluster_build_enabled()
                and await _image_exists_on_dockerhub(
                    f"{_image_registry()}/agenticnetwork-custom-{_image_slug(row['name'])}",
                    row["updated_at"],
                )
            )
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if redeployable:
                        # Keep the port (spawn_queue reuses it); clear the stale
                        # ads_cid (its `dir` entry was wiped). The host spawn loop
                        # picks this up and finalize_spawned republishes to ADS.
                        await conn.execute(
                            "UPDATE agents SET status = 'image_ready', ads_cid = NULL, "
                            "updated_at = now() WHERE id = $1",
                            row["id"],
                        )
                        requeued += 1
                    else:
                        # Not auto-redeployable (image gone or non-custom): free the
                        # port and mark stopped. init_db() purges stopped rows next run.
                        if row["port"]:
                            await release_port(conn, row["port"])
                        await conn.execute(
                            "UPDATE agents SET status = 'stopped', ads_cid = NULL, "
                            "updated_at = now() WHERE id = $1",
                            row["id"],
                        )
                        stopped += 1
            logger.info(
                "recovery_reconciled_cluster agent_id=%s name=%s -> %s",
                agent_id, row["name"], "image_ready" if redeployable else "stopped",
            )
        logger.info(
            "recovery_complete_cluster requeued=%d stopped=%d", requeued, stopped
        )

    # ── Health-check loop ──────────────────────────────────────────────────────

    async def start_health_loop(self) -> None:
        self._health_task = asyncio.create_task(self._health_loop(), name="supervisor-health")

    async def stop_health_loop(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEALTH_POLL_SECONDS)
            try:
                await self._check_all_live_agents()
            except Exception as exc:
                logger.exception("health_loop_error error=%s", exc)

    async def _check_all_live_agents(self) -> None:
        if isinstance(self._backend, ClusterBackend):
            # Lifecycle of cluster agents is owned by Slurm + the host spawn loop;
            # the supervisor doesn't health-restart them. (Known limitation: a
            # crashed cluster agent isn't auto-respawned — re-approve to redeploy.)
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM agents WHERE status = 'live'"
            )
        for row in rows:
            agent_id = str(row["id"])
            if not await self._backend.is_alive(agent_id):
                logger.warning("health_check_dead agent_id=%s triggering_restart", agent_id)
                asyncio.create_task(
                    self.restart_with_backoff(agent_id),
                    name=f"restart-{agent_id}",
                )

    # ── Build-poll loop (cluster custom builds) ──────────────────────────────────

    async def start_build_poll_loop(self) -> None:
        self._build_task = asyncio.create_task(self._build_poll_loop(), name="supervisor-build-poll")

    async def stop_build_poll_loop(self) -> None:
        if self._build_task:
            self._build_task.cancel()
            try:
                await self._build_task
            except asyncio.CancelledError:
                pass
            self._build_task = None

    async def _build_poll_loop(self) -> None:
        while True:
            await asyncio.sleep(_BUILD_POLL_SECONDS)
            try:
                await self._check_building_agents()
            except Exception as exc:
                logger.exception("build_poll_error error=%s", exc)

    async def _check_building_agents(self) -> None:
        if not _cluster_build_enabled():
            return
        building = await list_agents_by_status("building")
        for agent in building:
            slug = _image_slug(agent["name"])
            image = f"{_image_registry()}/agenticnetwork-custom-{slug}"
            if await _image_exists_on_dockerhub(image, agent.get("updated_at")):
                # Hand off to the host spawn loop: it polls /internal/spawn-queue for
                # image_ready agents, runs srun, and calls finalize_spawned (Note #8).
                await _set_status(str(agent["id"]), "image_ready")
                logger.info("custom_image_ready agent_id=%s image=%s -> image_ready", agent["id"], image)

    # ── Host-loop spawn coordination (cluster, Note #8) ──────────────────────────

    async def spawn_queue(self) -> list[dict[str, Any]]:
        """Agents ready for the host loop to srun (status='image_ready')."""
        if not isinstance(self._backend, ClusterBackend):
            return []
        out: list[dict[str, Any]] = []
        for agent in await list_agents_by_status("image_ready"):
            raw = agent["config"]
            config = raw if isinstance(raw, dict) else json.loads(raw)
            argv, env = cluster_srun_argv(
                agent_id=str(agent["id"]),
                name=agent["name"],
                description=agent["description"],
                handler_type=agent["handler_type"],
                config=config,
                port=agent["port"],
            )
            out.append({"agent_id": str(agent["id"]), "port": agent["port"], "argv": argv, "env": env})
        return out

    async def finalize_spawned(self, agent_id: str, pid: int) -> None:
        """Called by the host loop after a successful srun: record PID, publish, go live."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agents WHERE id = $1", uuid.UUID(agent_id))
        if row is None:
            logger.warning("finalize_spawned_unknown agent_id=%s", agent_id)
            return
        agent = dict(row)
        port: int = agent["port"]
        raw_skills = agent["oasf_skills"]
        oasf_skills = raw_skills if isinstance(raw_skills, list) else json.loads(raw_skills)

        if isinstance(self._backend, ClusterBackend):
            self._backend.record_pid(agent_id, pid, port)
        await self._publish_and_set_live(
            agent_id, agent["name"], agent["description"], oasf_skills, port,
            _declared_credentials(_deserialize_config(agent)),
        )
        logger.info("custom_spawned_live agent_id=%s pid=%s port=%s", agent_id, pid, port)

    def stop_queue(self) -> list[int]:
        """PIDs the host loop should kill (consumed once)."""
        if isinstance(self._backend, ClusterBackend):
            return self._backend.pending_stops()
        return []

    # ── GitHub commit on approval ────────────────────────────────────────────────

    async def delete_github_context(self, name: str) -> None:
        """Best-effort removal of a custom agent's committed build context.

        Only acts on the cluster build path (GITHUB_TOKEN/REPO + SPAWN_BACKEND set);
        a failure is logged, never raised, so it can't block agent deletion.
        """
        if not _cluster_build_enabled():
            return
        try:
            await _delete_from_github(_image_slug(name))
            logger.info("github_context_deleted name=%s", name)
        except Exception as exc:
            logger.warning("github_delete_failed name=%s error=%s", name, exc)

    async def _commit_to_github(self, agent: dict[str, Any], oasf_skills: list) -> None:
        from registration_service.builder import generate_card_json, generate_dockerfile

        repo = os.environ["GITHUB_REPO"]
        token = os.environ["GITHUB_TOKEN"]
        name = agent["name"]
        slug = _image_slug(name)

        raw_cfg = agent["config"]
        config = CustomConfig.model_validate(
            raw_cfg if isinstance(raw_cfg, dict) else json.loads(raw_cfg)
        )

        handler_path = agent.get("handler_file_path")
        if not handler_path or not Path(handler_path).exists():
            raise RuntimeError(f"Handler file missing for agent {agent['id']}")
        handler_dir = Path(handler_path).parent

        dockerfile = generate_dockerfile(config)
        card_json = json.dumps(
            generate_card_json({**agent, "oasf_skills": oasf_skills}), indent=2
        )

        base = f"registered_agents/{slug}"
        files = {
            f"{base}/Dockerfile": dockerfile,
            f"{base}/{slug}.card.json": card_json,
        }
        # The user's uploaded project tree → src/ (entry file at the src root, next
        # to importable siblings). Covers both single-file and multi-file uploads.
        for rel in list_tree(handler_dir):
            fpath = handler_dir / rel
            try:
                files[f"{base}/src/{rel}"] = fpath.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise RuntimeError(
                    f"Cannot commit binary file {rel!r} for agent {agent['id']}: the "
                    f"cluster build path currently supports text project files only."
                )
        # Vendor agent_skeleton into the build context so the image installs it from
        # a local copy (the private AgenticNetwork repo isn't reachable from CI, and
        # a token in the Dockerfile would be committed). See builder.generate_dockerfile.
        for rel, content in _vendored_skeleton_files().items():
            files[f"{base}/agent_skeleton/{rel}"] = content

        await _github_commit_files(
            repo, token, files, f"Register custom agent: {name}"
        )
        logger.info("github_committed agent_id=%s dir=%s files=%d", agent["id"], base, len(files))


def _vendored_skeleton_files() -> dict[str, str]:
    """Collect agent_skeleton's package files (all *.py + pyproject.toml) for vendoring.

    Keys are paths relative to the agent_skeleton/ dir (POSIX, with subdirs
    preserved, e.g. "system_tools/call_endpoint.py"); values are file contents.
    These are committed under registered_agents/{slug}/agent_skeleton/ so the
    generated image can `pip install ./agent_skeleton` with no external access.

    Must recurse: `agent_skeleton/__init__.py` imports `.system_tools.call_endpoint`
    at import time, so the subpackages have to be in the build context or every
    built image crashes on start. Skip test/build trees — they aren't installed
    (see pyproject `packages`) and only bloat the committed context.
    """
    _SKIP_DIRS = {"tests", "build", "dist", "__pycache__"}
    out: dict[str, str] = {}
    for path in sorted(_AGENT_SKELETON_DIR.rglob("*.py")):
        rel = path.relative_to(_AGENT_SKELETON_DIR)
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        out[rel.as_posix()] = path.read_text()
    pyproject = _AGENT_SKELETON_DIR / "pyproject.toml"
    if pyproject.exists():
        out["pyproject.toml"] = pyproject.read_text()
    return out


async def _github_commit_files(
    repo: str, token: str, files: dict[str, str], message: str
) -> None:
    """Commit multiple files in ONE commit via the Git Data API.

    A single atomic commit is important: the build workflow triggers on push to
    registered_agents/**, and committing files one at a time (Contents API) would
    fire the build before the Dockerfile exists. Tree entries use inline `content`,
    so no separate blob-creation round-trips are needed.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api = f"https://api.github.com/repos/{repo}"
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        # Resolve default branch → its head commit → its base tree.
        repo_info = (await client.get(api)).raise_for_status().json()
        branch = repo_info.get("default_branch", "main")
        ref = (await client.get(f"{api}/git/ref/heads/{branch}")).raise_for_status().json()
        base_commit_sha = ref["object"]["sha"]
        base_commit = (await client.get(f"{api}/git/commits/{base_commit_sha}")).raise_for_status().json()
        base_tree_sha = base_commit["tree"]["sha"]

        tree = [
            {"path": path, "mode": "100644", "type": "blob", "content": content}
            for path, content in files.items()
        ]
        new_tree = (
            await client.post(f"{api}/git/trees", json={"base_tree": base_tree_sha, "tree": tree})
        ).raise_for_status().json()
        new_commit = (
            await client.post(
                f"{api}/git/commits",
                json={"message": message, "tree": new_tree["sha"], "parents": [base_commit_sha]},
            )
        ).raise_for_status().json()
        (
            await client.patch(f"{api}/git/refs/heads/{branch}", json={"sha": new_commit["sha"]})
        ).raise_for_status()


async def _delete_from_github(slug: str) -> None:
    """Remove registered_agents/{slug}/ from the agent-directory repo in ONE commit.

    Git Data API: build a new tree off the base tree with every blob under the
    agent's dir marked for deletion (sha=None). No-op if the dir isn't present.
    (The deletion push won't trigger a spurious image build — the workflow's detect
    step skips changed dirs that no longer contain a Dockerfile.)
    """
    repo = os.environ["GITHUB_REPO"]
    token = os.environ["GITHUB_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api = f"https://api.github.com/repos/{repo}"
    prefix = f"registered_agents/{slug}/"
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        repo_info = (await client.get(api)).raise_for_status().json()
        branch = repo_info.get("default_branch", "main")
        ref = (await client.get(f"{api}/git/ref/heads/{branch}")).raise_for_status().json()
        base_commit_sha = ref["object"]["sha"]
        base_commit = (await client.get(f"{api}/git/commits/{base_commit_sha}")).raise_for_status().json()
        base_tree_sha = base_commit["tree"]["sha"]
        tree = (
            await client.get(f"{api}/git/trees/{base_tree_sha}", params={"recursive": "1"})
        ).raise_for_status().json()
        blobs = [
            it for it in tree.get("tree", [])
            if it.get("type") == "blob" and it["path"].startswith(prefix)
        ]
        if not blobs:
            return  # nothing committed for this agent
        deletions = [
            {"path": it["path"], "mode": it["mode"], "type": "blob", "sha": None}
            for it in blobs
        ]
        new_tree = (
            await client.post(f"{api}/git/trees", json={"base_tree": base_tree_sha, "tree": deletions})
        ).raise_for_status().json()
        new_commit = (
            await client.post(
                f"{api}/git/commits",
                json={"message": f"Remove custom agent: {slug}", "tree": new_tree["sha"], "parents": [base_commit_sha]},
            )
        ).raise_for_status().json()
        (
            await client.patch(f"{api}/git/refs/heads/{branch}", json={"sha": new_commit["sha"]})
        ).raise_for_status()


async def _image_exists_on_dockerhub(image_name: str, since: Any = None) -> bool:
    """True if {image_name}:latest exists on Docker Hub (public repo, no auth).

    When `since` (the agent's approval/update time) is provided, also require the
    tag's last_updated to be at or after it, so a stale image from a previous build
    doesn't trigger a premature spawn.
    """
    url = f"https://hub.docker.com/v2/repositories/{image_name}/tags/latest"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return False
        if since is None:
            return True
        last_updated = resp.json().get("last_updated")
        if not last_updated:
            return True
        tag_ts = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        since_ts = since if isinstance(since, datetime) else datetime.fromisoformat(str(since))
        if since_ts.tzinfo is None:
            since_ts = since_ts.replace(tzinfo=timezone.utc)
        return tag_ts >= since_ts
    except Exception as exc:
        logger.warning("dockerhub_poll_error image=%s error=%s", image_name, exc)
        return False


# ── Module-level singleton ─────────────────────────────────────────────────────

_supervisor: Supervisor | None = None


def get_supervisor() -> Supervisor:
    global _supervisor
    if _supervisor is None:
        import os
        if os.getenv("FAKE_DB"):
            from registration_service.fake_supervisor import FakeSupervisor
            _supervisor = FakeSupervisor()  # type: ignore[assignment]
        else:
            _supervisor = Supervisor()
    return _supervisor


# ── Helpers ────────────────────────────────────────────────────────────────────

def _declared_credentials(config: AnyConfig) -> list[dict[str, Any]]:
    """Declared user credentials (P4a) — only custom agents can declare them."""
    if isinstance(config, CustomConfig):
        return [c.model_dump() for c in config.credentials]
    return []


def _deserialize_config(agent: dict[str, Any]) -> AnyConfig:
    handler_type: str = agent["handler_type"]
    raw = agent["config"]
    config_data: dict = raw if isinstance(raw, dict) else json.loads(raw)
    if handler_type == "llm":
        return LLMConfig.model_validate(config_data)
    if handler_type == "custom":
        return CustomConfig.model_validate(config_data)
    if handler_type == "endpoint_ops":
        return EndpointOpsConfig.model_validate(config_data)
    return APIProxyConfig.model_validate(config_data)


def _build_agent_card(
    agent_id: str,
    name: str,
    description: str,
    oasf_skills: list[dict[str, Any]],
    port: int,
    credentials: list[dict[str, Any]] | None = None,
) -> AgentCard:
    skills = [
        AgentSkill(
            id=s.get("key") or s["name"],   # prefer canonical key; fall back to name
            name=s["name"].split("/")[-1],
            description=description,
            # Self-describe the ADS routing skill via an 'oasf:<name>:<id>' tag
            # so the card is discoverable without a per-agent entry in
            # ads_utils.converter's routing map.
            tags=(
                [oasf_skill_tag(s.get("key") or s["name"], s["id"])]
                if s.get("id") is not None
                else []
            ),
        )
        for s in oasf_skills
    ]
    # P4a: declared user credentials ride on capabilities.extensions (the only
    # field the planner's discovery read path round-trips). Omitted when empty.
    ext = build_credentials_extension(credentials)
    return AgentCard(
        name=name,
        description=description,
        url=f"http://localhost:{port}/",
        version="1.0.0",
        # streaming=True so the planner uses SSE and honors the executor's <30s
        # heartbeat (P3) — this is the card the planner actually reads from ADS.
        capabilities=AgentCapabilities(
            streaming=True,
            extensions=[ext] if ext else None,
        ),
        skills=skills,
        default_input_modes=["text"],
        default_output_modes=["text"],
    )


async def _set_status(
    agent_id: str,
    status: str,
    *,
    endpoint_url: str | None = None,
    ads_cid: str | None = None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agents
            SET status = $1,
                endpoint_url = COALESCE($2, endpoint_url),
                ads_cid = COALESCE($3, ads_cid),
                updated_at = now()
            WHERE id = $4
            """,
            status,
            endpoint_url,
            ads_cid,
            uuid.UUID(agent_id),
        )
