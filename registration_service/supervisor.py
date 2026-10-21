"""Process supervisor — spawn, publish, monitor, and recover registered agents."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import uuid
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from agentnet.directory.card_oasf import oasf_skill_tag
from agentnet.directory.publisher import DirectoryPublisher, DirectoryUnavailable
from a2a.types import AgentCard, AgentCapabilities, AgentSkill

from registration_service.card_credentials import build_credentials_extension
from registration_service.card_resources import build_resources_extension

from registration_service.archive import list_tree, tree_revision
from registration_service.backend import (
    _AGENT_SKELETON_DIR,
    ContainerBackend,
    _image_registry,
    _image_slug,
    cluster_srun_argv,
    make_backend,
)
from registration_service.db import get_agent, get_pool, list_agents_by_status, update_agent
from registration_service.flags import fake_supervisor_enabled
from registration_service.models import (
    AnyConfig,
    CustomConfig,
    carries_upload,
    parse_config,
)
from registration_service.oasf_skills import has_routable_skill
from registration_service.port_pool import release_port

logger = logging.getLogger(__name__)

# "Does this agent ship uploaded Python?" — the gate for admin approval before spawn
# and (on the cluster) for the per-agent image build via the GitHub → Docker Hub
# pipeline. It is a property of the CONFIG (`carries_upload`), not of the
# handler_type name: both `custom` and `llm` with `tools.source='upload'` qualify.

_HEALTH_POLL_SECONDS = 30.0
_BUILD_POLL_SECONDS = 30.0
_BACKOFF_BASE_SECONDS = 5.0
_MAX_RESTART_ATTEMPTS = 3

# One lock per agent. Every change to an agent's code, approval or launch holds
# it, so they run one at a time: the API's registration, upload, source review,
# approve, reject, restart and delete (server.py), and the supervisor's own
# launches (a health-loop restart attempt, the build poll's move to
# 'image_ready', a Cloud Run deploy, and the cluster host loop's report of a
# started process). Startup recovery needs no lock (the Cloud Run deploy it
# runs takes one anyway): it runs before the API serves and before the health
# and build loops start. Keyed by the canonical id (the UUID's str), so every
# spelling of the id shares one lock. An entry lives only while a holder or
# waiter references its lock.
_agent_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _agent_lock(agent_id: str) -> asyncio.Lock:
    return _agent_locks.setdefault(str(agent_id), asyncio.Lock())


def _backend_flag(backend: object, name: str, default: bool) -> bool:
    """Read a ContainerBackend capability flag defensively.

    Uses getattr rather than a direct attribute access so backends that don't
    subclass ContainerBackend (e.g. test fakes) still get the documented default
    instead of an AttributeError.
    """
    return getattr(backend, name, default)


def _oob_build_enabled() -> bool:
    """True when custom approvals should go through the GitHub → Docker Hub build.

    Both out-of-band-build backends use this pipeline: the ENGR cluster and Cloud
    Run. They diverge only at launch (host-loop srun vs. Cloud Run Admin API), not
    at build. (`owns_build` is the per-backend flag; this env gate additionally
    requires the GitHub credentials the commit step needs.)
    """
    return (
        os.getenv("SPAWN_BACKEND") in ("cluster", "cloudrun")
        and bool(os.getenv("GITHUB_TOKEN"))
        and bool(os.getenv("GITHUB_REPO"))
    )


class LaunchRefused(RuntimeError):
    """The backend cannot start the agent in its current state or configuration.
    Raised before anything is started or changed; ``status_code`` is the HTTP
    status the API answers with (409 for the agent's state, 503 for missing
    configuration)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code


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
        # Health-loop restarts in flight, one per agent: backoff plus spawn can
        # outlast a health poll, and a second restart would kill the first's
        # half-started process. Held here so the tasks aren't garbage-collected
        # and can be cancelled on shutdown.
        self._restart_tasks: dict[str, asyncio.Task[None]] = {}
        # dirctl calls are blocking subprocess runs: they go to a worker thread so
        # /health stays responsive, one at a time (a sqlite-backed dir daemon
        # answers SQLITE_BUSY to concurrent writes, e.g. recovery's gather).
        self._directory_lock = asyncio.Lock()

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

        # Uploaded-code agents must be explicitly approved before they can spawn —
        # they run arbitrary author Python.
        uploads_code = carries_upload(config)
        if uploads_code and not agent.get("approved", False):
            raise RuntimeError(f"Agent {agent_id} is not approved")

        # Belt-and-suspenders: the API layer (server.register_agent) already
        # rejects a skill list with no routable id, but a row can also reach here
        # via the cluster host-loop / recovery / a future CloudRunBackend path, or
        # simply predate that check. Catch it before spawning anything — publish
        # would otherwise raise ValueError (uncaught by _publish_and_set_live,
        # which only handles DirectoryUnavailable) *after* the backend is already
        # running, leaving an orphaned process with no card and no 'live' status.
        if not has_routable_skill(oasf_skills):
            await _set_status(agent_id, "failed")
            raise RuntimeError(
                f"Agent {agent_id} has no OASF skill with an id — it would publish "
                "to ADS stored-but-not-routed (undiscoverable). Fix its oasf_skills."
            )

        # Out-of-band build (cluster, Cloud Run): uploaded code runs only from the
        # image built for its approval. The approval commits the build context to
        # agent-directory and waits (status='building') for the image on Docker
        # Hub; the build poll marks it 'image_ready', and the host loop (cluster,
        # finalize_spawned, see Note #8) or the Cloud Run deploy
        # (_deploy_ready_agent) launches it. There is no direct spawn here: on
        # the cluster spawn() starts nothing, and on Cloud Run it would deploy
        # whatever ':latest' holds. So an agent that is not waiting for its first
        # build (one already building or launching, or a live, failed or stopped
        # one) is refused; a restart sends it through the build again.
        owns_build = _backend_flag(self._backend, "owns_build", False)
        if uploads_code and (owns_build or _oob_build_enabled()):
            if not _oob_build_enabled():
                raise LaunchRefused(
                    503,
                    f"Agent {agent_id} ships uploaded code, which this backend runs only "
                    "from a built image: set GITHUB_TOKEN and GITHUB_REPO for the build.",
                )
            if agent.get("status") != "registered":
                raise LaunchRefused(
                    409,
                    f"Agent {agent_id} is {agent.get('status')!r}, not waiting for a "
                    "build; restart it to rebuild and redeploy.",
                )
            await self._commit_to_github(agent, oasf_skills)
            await _set_status(agent_id, "building")
            logger.info("custom_build_started agent_id=%s name=%s", agent_id, name)
            return

        # An out-of-band-build backend (cluster, Cloud Run) only has a spawn path
        # for UPLOADED-CODE agents (a per-agent image built via the GitHub→Docker
        # Hub flow). A prompt-only `llm` or a `proxy` has no such path: spawn() is
        # a no-op and the host loop only launches built ('image_ready') agents.
        # Without this guard such an agent would be published to ADS and marked
        # 'live' with no process behind it (a ghost-live row the planner would then
        # dispatch to). Fail loudly instead of creating that phantom.
        if owns_build and not uploads_code:
            await _set_status(agent_id, "failed")
            raise RuntimeError(
                f"handler_type={handler_type!r} with this config is not supported on "
                "the cluster backend — only uploaded-code agents have a cluster spawn "
                "path (a 'custom' handler, or an 'llm' agent whose tools.source is "
                "'upload'). Register it with the local subprocess backend instead."
            )

        # A re-spawn (health-loop restart, startup recovery) still has the previous
        # process's card in the directory. Drop it first: it points at a dead
        # process, and the new card may hash to the same CID, so unpublishing
        # after the new publish could remove the new one. If the directory is
        # down the row keeps the CID: the card is still there, and the next
        # publish (or a later stop) replaces or retries it.
        await self._remove_card(agent_id, agent.get("ads_cid"))

        try:
            # An off-host backend (Cloud Run) returns the agent's URL; local and
            # cluster backends return None and the card advertises localhost:port.
            endpoint_url = await self._backend.spawn(
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
            agent_id, name, description, oasf_skills, port, _declared_credentials(config),
            endpoint_url=endpoint_url,
        )

    async def _publish_and_set_live(
        self,
        agent_id: str,
        name: str,
        description: str,
        oasf_skills: list,
        port: int,
        credentials: list[dict[str, Any]] | None = None,
        endpoint_url: str | None = None,
        resources: dict[str, Any] | None = None,
    ) -> None:
        """Publish the agent card to ADS and mark it live. Shared by the local-spawn
        path (spawn_and_publish), the cluster host-loop path (finalize_spawned), and
        the Cloud Run deploy path (_deploy_ready_agent).

        endpoint_url overrides the advertised URL — an off-host backend (Cloud Run)
        passes its run.app URL; the local/cluster paths default to localhost:port.
        resources surfaces the agent's declared sizing on the card (Step 8).

        Every caller holds the agent's lock (_agent_lock) and has checked the
        row under it (approved, and for a built image still the launch the row
        waits for), so no reject, upload or delete lands between that check and
        the 'live' write. (Startup recovery runs before the API serves.)"""
        row = await get_agent(agent_id)
        old_cid = row.get("ads_cid")

        endpoint_url = endpoint_url or f"http://localhost:{port}/"
        card = _build_agent_card(
            agent_id, name, description, oasf_skills, port, credentials, resources
        )

        ads_cid: str | None = None
        try:
            record = await self._directory_call(
                self._publisher.publish, card, advertise_url=endpoint_url
            )
            ads_cid = record.backend_ref
            logger.info("ads_published agent_id=%s cid=%s", agent_id, ads_cid)
        except DirectoryUnavailable as exc:
            # A record stored before the failure may still be routed later: the
            # row keeps its CID, so a stop or delete removes it.
            ads_cid = exc.cid
            logger.warning("ads_publish_skipped agent_id=%s cid=%s reason=%s", agent_id, ads_cid, str(exc).replace("\n", " | "))

        # The row still records an earlier card that could not be removed before
        # (a different CID; an equal one is the card just published). The
        # directory is reachable now, so retry; the new CID replaces it either way.
        if old_cid and ads_cid and old_cid != ads_cid and not await self._unpublish(agent_id, old_cid):
            logger.warning("ads_stale_card_left agent_id=%s cid=%s replaced_by=%s", agent_id, old_cid, ads_cid)

        await _set_status(agent_id, "live", endpoint_url=endpoint_url, ads_cid=ads_cid)

    async def _directory_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        async with self._directory_lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def _unpublish(self, agent_id: str, ads_cid: str) -> bool:
        """Best-effort card removal; True if the directory confirmed it. A
        directory outage is logged, not raised."""
        try:
            return bool(await self._directory_call(self._publisher.unpublish, ads_cid))
        except DirectoryUnavailable as exc:
            logger.warning("ads_unpublish_failed agent_id=%s cid=%s error=%s", agent_id, ads_cid, exc)
            return False

    async def _remove_card(self, agent_id: str, cid: str | None) -> bool:
        """Unpublish ``cid`` and clear the row's ads_cid once the directory
        confirms. True if no card is left; otherwise the row keeps the CID, so
        a later stop, publish or startup retries it."""
        if not cid:
            return True
        if not await self._unpublish(agent_id, cid):
            return False
        await update_agent(agent_id, ads_cid=None)
        return True

    async def shutdown(self) -> None:
        """Stop the agent processes this service owns, then drop their cards.

        Subprocess agents die with the service, so their cards would otherwise
        advertise dead endpoints (or, after the next start reuses the port, a
        different agent). Rows keep status 'live' so startup recovery re-spawns
        them; ads_cid is cleared once the card is gone (kept if the directory was
        down, so recovery retries the unpublish). Backends whose agents outlive
        this process (cluster, Cloud Run) stop nothing and return [].
        """
        stop_all = getattr(self._backend, "stop_all", None)
        stopped: list[str] = await stop_all() if stop_all else []
        for agent_id in stopped:
            agent = await get_agent(agent_id)
            if agent:
                await self._remove_card(agent_id, agent.get("ads_cid"))
        if stopped:
            logger.info("supervisor_shutdown stopped=%d", len(stopped))

    async def stop_and_unpublish(self, agent_id: str) -> bool:
        """Unpublish from ADS, stop the subprocess, release the port. Returns
        True if no card is left in the directory.

        The caller holds the agent's lock (_agent_lock), so a health-loop
        restart pending for the agent is sleeping or waiting for that lock,
        never in the middle of an attempt; it is cancelled first. The row keeps
        its ads_cid if the directory did not confirm the removal: the card is
        still there, and startup recovery (or the next stop) retries it.
        """
        restart = self._restart_tasks.pop(agent_id, None)
        if restart is not None:
            restart.cancel()
            await asyncio.gather(restart, return_exceptions=True)

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ads_cid, port FROM agents WHERE id = $1", uuid.UUID(agent_id)
            )
        if row is None:
            return True

        port: int | None = row["port"]
        # Removed (and the CID cleared) before the stop, so a failing
        # backend.stop below cannot leave the row holding a card that is gone.
        removed = await self._remove_card(agent_id, row["ads_cid"])

        await self._backend.stop(agent_id)

        if port:
            async with pool.acquire() as conn:
                await release_port(conn, port)
        await update_agent(agent_id, status="stopped")
        logger.info("agent_stopped agent_id=%s card_removed=%s", agent_id, removed)
        return removed

    async def _retry_unpublish_stale_cards(self) -> None:
        """Remove cards still recorded on rows that are not live, or that lost
        their approval: a stop, revoke or respawn that ran while the directory
        was unreachable, or an earlier version's upload that kept the status.
        Each CID is cleared once the directory confirms; otherwise it stays for
        the next startup.

        An approved 'building' row keeps its CID: clearing it would move
        updated_at, which the build poll compares the image's push time against.
        That card is removed when the agent publishes (the old-CID retry in
        _publish_and_set_live) or is stopped.

        If the directory does not answer a quick probe, the pass is skipped, so
        an outage does not hold up startup by one dirctl timeout per card.

        On the cluster the registration step starts only with the Slurm job,
        after the job has recreated the directory's database (`dir`) and its
        container: every recorded card is already gone and its CID is just
        cleared. A restart of the registration service alone within a job would
        need `_remove_card` here instead.
        """
        stale = [
            agent
            for status in ("stopped", "failed", "registered", "image_ready", "building", "live")
            for agent in await list_agents_by_status(status)
            if agent.get("ads_cid") and (status not in ("building", "live") or not agent["approved"])
        ]
        if not stale:
            return
        if _backend_flag(self._backend, "needs_host_loop", False):
            for agent in stale:
                await update_agent(str(agent["id"]), ads_cid=None)
            return
        try:
            await self._directory_call(self._publisher.healthcheck)
        except DirectoryUnavailable as exc:
            logger.warning("ads_stale_cards_kept count=%d reason=%s", len(stale), exc)
            return
        for agent in stale:
            await self._remove_card(str(agent["id"]), agent["ads_cid"])

    async def _reset_revoked_rows(self) -> None:
        """Return to the pre-approval state each row that lost its approval while
        something may still run for it: one that is not 'registered' or
        'stopped', or 'registered' with an image. A revoke whose backend stop
        raised leaves such a row, and so did earlier versions' upload and reject,
        which kept the status or the service. What the backend runs for the
        agent is stopped and the row set 'registered' with no image; its card
        went in the sweep before. A stop that raises is logged and the row left
        as it is, for the next startup or an admin's reject."""
        for status in ("live", "failed", "building", "image_ready", "registered"):
            for agent in await list_agents_by_status(status):
                if agent["approved"] or (status == "registered" and not agent.get("image_ref")):
                    continue
                agent_id = str(agent["id"])
                try:
                    await self._return_to_approval(agent_id)
                except Exception as exc:
                    logger.error("recovery_revoke_failed agent_id=%s error=%s", agent_id, exc)
                    continue
                logger.info("recovery_revoked agent_id=%s %s -> registered", agent_id, status)

    async def _return_to_approval(self, agent_id: str) -> None:
        """Stop what the backend runs for an agent whose approval was revoked and
        set the pre-approval state, so nothing relaunches or queues it."""
        await self._backend.stop(agent_id)
        await update_agent(agent_id, status="registered", image_ref=None)

    async def restart_with_backoff(self, agent_id: str) -> None:
        """Attempt up to _MAX_RESTART_ATTEMPTS restarts with exponential backoff.

        Each attempt holds the agent's lock (the backoff sleep does not), so a
        reject, upload, restart or delete waits for an attempt in progress and
        then sees the card it published. An attempt first re-reads the row and
        stops restarting, leaving the status alone, if the agent was deleted,
        stopped, re-registered or sent to the build, lost its approval, or is
        live and running again (a restart through the API). A row still 'live'
        without approval (a revoke whose backend stop raised) is returned to the
        pre-approval state instead. Sets status='failed' if all attempts fail.
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
            async with _agent_lock(agent_id):
                row = await get_agent(agent_id)
                if row is not None and row["status"] == "live" and not row["approved"]:
                    await self._remove_card(agent_id, row.get("ads_cid"))
                    await self._return_to_approval(agent_id)
                    logger.info("restart_revoked agent_id=%s -> registered", agent_id)
                    return
                if (
                    row is None
                    or row["status"] not in ("live", "failed")
                    or not row["approved"]
                    or (row["status"] == "live" and await self._backend.is_alive(agent_id))
                ):
                    logger.info("restart_not_needed agent_id=%s", agent_id)
                    return
                await self._backend.stop(agent_id)
                try:
                    await self.spawn_and_publish(agent_id)
                    logger.info("restart_succeeded agent_id=%s attempt=%d", agent_id, attempt)
                    return
                except Exception as exc:
                    logger.warning(
                        "restart_failed agent_id=%s attempt=%d error=%s", agent_id, attempt, exc
                    )
                if attempt == _MAX_RESTART_ATTEMPTS:
                    logger.error("restart_exhausted agent_id=%s", agent_id)
                    await _set_status(agent_id, "failed")

    async def recover_live_agents(self) -> None:
        """On startup, reconcile agents that were 'live' before the last shutdown.
        Three backends, three recovery models (keyed on capability flags):

        Subprocess: the agents were in-process children that died with this
        process — re-spawn them in-process.

        Cluster (`needs_host_loop`): the actual agent processes were independent
        srun steps that died with the previous Slurm job, and ADS's routing index
        (`dir`) is wiped fresh each deploy — so a persisted status='live' row is a
        *ghost* (no process, not routed, invisible on the planner dashboard). The
        registration container can't re-spawn srun steps itself, but it can
        reconcile the DB to reality: re-queue uploaded-code agents (`custom`, or
        `llm` with uploaded tools: the same `_uploads_code` predicate that gates
        approval and the image build) whose image still exists on Docker Hub back
        to 'image_ready' (the host spawn loop then redeploys + republishes them),
        and mark the rest 'stopped'. Agents that upload no code have no image and
        no cluster spawn path, so their 'live' rows are always ghosts.

        Cloud (`publishes_decoupled`): Cloud Run services and the ADS card (on a
        separate VM) persist independently of this singleton, so a 'live' row is
        usually still real. Reconcile against actual services via the backend API
        (see `_recover_cloud_agents`) rather than re-spawning blindly.

        First, on every backend: retry removing cards that rows which are not live,
        or lost their approval, still record (`_retry_unpublish_stale_cards`), then
        stop what runs for rows that lost their approval and return them to
        'registered' (`_reset_revoked_rows`). The branches below see only
        approved rows (or unapproved ones whose stop failed).
        """
        pool = await get_pool()

        await self._retry_unpublish_stale_cards()
        await self._reset_revoked_rows()

        # Cloud: agents + their decoupled cards outlive this process — reconcile via
        # the API instead of re-spawning. (Checked before the subprocess branch,
        # which is also needs_host_loop=False.)
        if _backend_flag(self._backend, "publishes_decoupled", False):
            await self._recover_cloud_agents()
            return

        if not _backend_flag(self._backend, "needs_host_loop", False):
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
                "SELECT id, name, handler_type, config, port, updated_at "
                "FROM agents WHERE status = 'live'"
            )
        if not rows:
            logger.info("recovery_skipped_cluster no_live_agents")
            return

        requeued = 0
        stopped = 0
        for row in rows:
            agent_id = str(row["id"])
            # NB: do NOT pass row["updated_at"] as `since` here. `updated_at` is
            # bumped to now() on every status change (see _set_status), including
            # when the agent went 'live' — which always happens *after* the image
            # was built and pushed. So a freshness check would compare the image's
            # build time against the (later) go-live time and always fail, wrongly
            # marking every recoverable agent 'stopped'. On recovery we only need to
            # know the image still exists; the `since` guard belongs in the
            # build-poll path (_check_building_agents), where updated_at really is
            # the approval time and a newer image is what we're waiting for.
            redeployable = (
                _uploads_code(dict(row))
                and _oob_build_enabled()
                and await _image_exists_on_dockerhub(
                    f"{_image_registry()}/agenticnetwork-custom-{_image_slug(row['name'])}",
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
                        # Not auto-redeployable (image gone or no uploaded code): free the
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

    async def _recover_cloud_agents(self) -> None:
        """Reconcile a decoupled-publish (Cloud Run) backend against reality.

        This singleton lost its in-memory state on restart, but the Cloud Run
        services and the ADS card (on a separate VM) persist independently — so we
        reconcile the DB rather than re-spawn:
          * 'live'        — confirm the service still exists & is Ready via the
                            backend API (`is_alive`, an Admin-API get — never an
                            HTTP probe, which would cold-start it). If so, leave it:
                            the decoupled card is still published. If the service
                            vanished, drop the stale card and redeploy.
          * 'image_ready' — the deploy trigger never fired before the restart (the
                            Step-4 window between image_ready and deploy); deploy now.
          * 'failed'      — but only if it has an image_ref: it built successfully and
                            failed at (or after) the deploy step, so retry it. A row
                            that failed *before* a build has no image_ref and is left
                            alone. Deploy failures shouldn't strand an agent forever.
        Only approved rows: _reset_revoked_rows returned the others to
        'registered', and one still here is one whose stop failed just now.
        """

        async def approved(status: str) -> list[dict[str, Any]]:
            return [a for a in await list_agents_by_status(status) if a["approved"]]

        confirmed = redeployed = 0
        for agent in await approved("live"):
            agent_id = str(agent["id"])
            if await self._backend.is_alive(agent_id):
                confirmed += 1
                logger.info("recovery_confirmed_cloud agent_id=%s name=%s", agent_id, agent["name"])
                continue
            # Service is gone — drop the stale card (best-effort) then redeploy. The
            # CID stays on the row until the directory confirms the removal.
            await self._remove_card(agent_id, agent.get("ads_cid"))
            logger.info("recovery_redeploy_cloud agent_id=%s name=%s (service gone)", agent_id, agent["name"])
            await self._deploy_ready_agent(agent)
            redeployed += 1
        for agent in await approved("image_ready"):
            logger.info(
                "recovery_deploy_stranded_cloud agent_id=%s name=%s (image_ready)",
                agent["id"], agent["name"],
            )
            await self._deploy_ready_agent(agent)
            redeployed += 1
        for agent in await approved("failed"):
            if not agent.get("image_ref"):
                continue  # failed before a build → not a deploy retry, leave it
            logger.info(
                "recovery_retry_failed_cloud agent_id=%s name=%s (built, deploy had failed)",
                agent["id"], agent["name"],
            )
            await self._deploy_ready_agent(agent)
            redeployed += 1
        logger.info("recovery_complete_cloud confirmed=%d redeployed=%d", confirmed, redeployed)

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
        # A restart still in its backoff would otherwise spawn an agent after
        # shutdown() stopped the others. Cancelling mid-spawn stops that process.
        restarts = list(self._restart_tasks.values())
        for task in restarts:
            task.cancel()
        await asyncio.gather(*restarts, return_exceptions=True)
        self._restart_tasks.clear()

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEALTH_POLL_SECONDS)
            try:
                await self._check_all_live_agents()
            except Exception as exc:
                logger.exception("health_loop_error error=%s", exc)

    async def _check_all_live_agents(self) -> None:
        if not _backend_flag(self._backend, "supports_health_restart", True):
            # Lifecycle is owned externally (Slurm + the host spawn loop for
            # cluster; Cloud Run self-heals) — the supervisor doesn't health-restart
            # these. (Known limitation for cluster: a crashed agent isn't
            # auto-respawned — restart it to rebuild and redeploy.)
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM agents WHERE status = 'live'"
            )
        for row in rows:
            agent_id = str(row["id"])
            if agent_id in self._restart_tasks:
                continue  # already restarting (backoff + spawn outlast a poll)
            if not await self._backend.is_alive(agent_id):
                logger.warning("health_check_dead agent_id=%s triggering_restart", agent_id)
                task = asyncio.create_task(
                    self.restart_with_backoff(agent_id),
                    name=f"restart-{agent_id}",
                )
                self._restart_tasks[agent_id] = task
                task.add_done_callback(functools.partial(self._forget_restart, agent_id))

    def _forget_restart(self, agent_id: str, task: asyncio.Task[None]) -> None:
        if self._restart_tasks.get(agent_id) is task:
            del self._restart_tasks[agent_id]

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
        if not _oob_build_enabled():
            return
        building = await list_agents_by_status("building")
        for agent in building:
            slug = _image_slug(agent["name"])
            image = f"{_image_registry()}/agenticnetwork-custom-{slug}"
            if await _image_exists_on_dockerhub(image, agent.get("updated_at")):
                # Best-effort digest capture: a failed lookup leaves image_ref=None
                # (the CloudRunBackend deploy then falls back to ':latest') rather
                # than blocking the transition to image_ready.
                image_ref = await _image_digest_on_dockerhub(image)
                # A reject or re-upload (and maybe a new approval) can land while
                # Docker Hub answers: advance only the row as it was listed.
                async with _agent_lock(str(agent["id"])):
                    current = await get_agent(agent["id"])
                    if not (
                        current
                        and current["status"] == "building"
                        and current.get("approved")
                        and current["updated_at"] == agent["updated_at"]
                    ):
                        continue
                    await update_agent(str(agent["id"]), status="image_ready", image_ref=image_ref)
                logger.info(
                    "custom_image_ready agent_id=%s image=%s image_ref=%s -> image_ready",
                    agent["id"], image, image_ref,
                )
                # Launch. Two shapes of out-of-band-build backend:
                #  * cluster (needs_host_loop=True): hand off to the host spawn loop
                #    — it polls /internal/spawn-queue, runs srun, calls finalize_spawned.
                #  * Cloud Run (needs_host_loop=False): no host loop; deploy via the
                #    Admin API right here and publish the run.app card.
                if _backend_flag(self._backend, "owns_build", False) and not _backend_flag(
                    self._backend, "needs_host_loop", False
                ):
                    await self._deploy_ready_agent(agent)

    async def _deploy_ready_agent(self, agent: dict[str, Any]) -> None:
        """Deploy an image_ready agent through a direct-API backend (Cloud Run) and
        publish its card. The cluster path defers this to the host srun loop; this
        is the no-host-loop equivalent.

        The deploy holds the agent's lock from the row check to the 'live' (or
        'failed') write, so a reject, upload or delete waits for it and then
        stops what it deployed. A row that lost its approval, was deleted, or was
        stopped or sent back through the build since it was listed is not
        deployed: the change that did so has already stopped the agent. A deploy
        that fails deletes the service it may have created.

        NOTE: a restart between the image_ready transition and a successful deploy
        would strand the row at image_ready (the build poll only scans 'building').
        Step 6 (recover_live_agents reconcile) closes that window; for now the
        deploy fires inline in the same poll iteration.
        """
        agent_id = str(agent["id"])
        async with _agent_lock(agent_id):
            agent = await get_agent(agent_id)
            if agent is None or not agent["approved"] or agent["status"] not in ("image_ready", "live", "failed"):
                logger.info("cloud_deploy_skipped agent_id=%s status=%s", agent_id, agent and agent["status"])
                return
            name = agent["name"]
            description = agent["description"]
            handler_type = agent["handler_type"]
            port: int = agent["port"]
            config = _deserialize_config(agent)
            raw_skills = agent["oasf_skills"]
            oasf_skills = raw_skills if isinstance(raw_skills, list) else json.loads(raw_skills)
            raw_res = agent.get("resources")
            resources = json.loads(raw_res) if isinstance(raw_res, str) else raw_res

            # Same belt-and-suspenders as spawn_and_publish: never deploy an agent that
            # would publish stored-but-not-routed (undiscoverable) — fail before the
            # backend creates a live-but-invisible service.
            if not has_routable_skill(oasf_skills):
                logger.error("cloud_deploy_unroutable agent_id=%s -> failed", agent_id)
                await _set_status(agent_id, "failed")
                return

            try:
                endpoint_url = await self._backend.spawn(
                    agent_id=agent_id,
                    name=name,
                    description=description,
                    handler_type=handler_type,
                    config=config,
                    port=port,
                )
            except Exception as exc:
                logger.error("cloud_deploy_spawn_failed agent_id=%s error=%s", agent_id, exc)
                # A failure after Cloud Run created the service (granting the
                # invoker, a revision that never becomes ready) leaves it running.
                # Delete it: the startup purge can remove a failed row without an
                # image_ref, and nothing would delete the service after that.
                try:
                    await self._backend.stop(agent_id)
                except Exception as stop_exc:
                    logger.error("cloud_deploy_cleanup_failed agent_id=%s error=%s", agent_id, stop_exc)
                await _set_status(agent_id, "failed")
                return

            await self._publish_and_set_live(
                agent_id, name, description, oasf_skills, port,
                _declared_credentials(config), endpoint_url=endpoint_url, resources=resources,
            )
        logger.info("cloud_deployed_live agent_id=%s url=%s", agent_id, endpoint_url)

    # ── Host-loop spawn coordination (cluster, Note #8) ──────────────────────────

    async def spawn_queue(self) -> list[dict[str, Any]]:
        """Agents ready for the host loop to srun: approved and 'image_ready'.
        (A reject or upload revokes approval before it moves the row back.)
        A row whose launch was reported is left out while that report publishes
        (finalize_spawned records the process first): a second launch would
        compete with the first for the agent's port."""
        if not _backend_flag(self._backend, "needs_host_loop", False):
            return []
        out: list[dict[str, Any]] = []
        for agent in await list_agents_by_status("image_ready"):
            if not agent["approved"] or self._backend.has_process(str(agent["id"])):
                continue
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
        """Called by the host loop after a successful srun: record PID, publish, go live.

        Holds the agent's lock, so a reject, upload or delete waits for the
        publish and then stops the agent. Only an approved row waiting for its
        launch ('image_ready') takes the reported process. Any other report is
        stopped: its PID is queued for the host loop to kill, and the process
        recorded for the agent, if any, is left as it is. That covers an agent
        deleted, rejected or superseded after the host loop took it off the
        queue (the change that did so already stopped what ran for it), and a
        repeated report for a row already live. If the publish fails with
        anything but a directory outage (an invalid card, a database error),
        the taken process is stopped, the row set 'failed' and the error
        raised. Only the cluster backend has a host loop; on the others a
        report is ignored.
        """
        if not _backend_flag(self._backend, "needs_host_loop", False):
            logger.warning("finalize_spawned_ignored agent_id=%s pid=%s (no host loop)", agent_id, pid)
            return
        async with _agent_lock(agent_id):
            agent = await get_agent(agent_id)
            if agent is None or not agent["approved"] or agent["status"] != "image_ready":
                self._backend.discard_pid(pid)
                logger.info(
                    "spawn_report_discarded agent_id=%s pid=%s status=%s",
                    agent_id, pid, agent and agent["status"],
                )
                return
            port: int = agent["port"]
            self._backend.record_pid(agent_id, pid, port)
            raw_skills = agent["oasf_skills"]
            oasf_skills = raw_skills if isinstance(raw_skills, list) else json.loads(raw_skills)
            try:
                await self._publish_and_set_live(
                    agent_id, agent["name"], agent["description"], oasf_skills, port,
                    _declared_credentials(_deserialize_config(agent)),
                )
            except Exception as exc:
                # Nothing relaunches or stops a recorded process left unpublished.
                logger.error("spawn_report_publish_failed agent_id=%s pid=%s error=%s", agent_id, pid, exc)
                await self._backend.stop(agent_id)
                await _set_status(agent_id, "failed")
                raise
        logger.info("custom_spawned_live agent_id=%s pid=%s port=%s", agent_id, pid, port)

    def stop_queue(self) -> list[int]:
        """PIDs the host loop should kill (consumed once)."""
        if _backend_flag(self._backend, "needs_host_loop", False):
            return self._backend.pending_stops()
        return []

    # ── GitHub commit on approval ────────────────────────────────────────────────

    async def delete_github_context(self, name: str) -> None:
        """Best-effort removal of a custom agent's committed build context.

        Only acts on the cluster build path (GITHUB_TOKEN/REPO + SPAWN_BACKEND set);
        a failure is logged, never raised, so it can't block agent deletion.
        """
        if not _oob_build_enabled():
            return
        try:
            await _delete_from_github(_image_slug(name))
            logger.info("github_context_deleted name=%s", name)
        except Exception as exc:
            logger.warning("github_delete_failed name=%s error=%s", name, exc)

    async def _commit_to_github(self, agent: dict[str, Any], oasf_skills: list) -> None:
        from registration_service.build_manifest import MANIFEST_FILENAME, build_agent_manifest
        from registration_service.builder import generate_card_json, generate_dockerfile

        repo = os.environ["GITHUB_REPO"]
        token = os.environ["GITHUB_TOKEN"]
        name = agent["name"]
        slug = _image_slug(name)

        # Either uploaded-code shape; generate_dockerfile reads the build fields
        # (entry_file, requirements, system_packages, …) via models.upload_spec.
        config = _deserialize_config(agent)

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
            # The authoring fields the build context otherwise loses (class_name,
            # max_runtime_seconds, declared credentials): without them the committed
            # tree can't be reconstructed and the credential declarations never get
            # PR review. Secret-bearing and free-text fields are NOT included — see
            # build_manifest for the allowlist and its commit-time self-test.
            #
            # If that self-test trips, this raises while the files dict is still being
            # assembled — before any network call — so nothing is committed. The
            # approval endpoint surfaces a 500 and the row stays 'registered', which
            # is retryable once the offending input is corrected. The exception text
            # never echoes the offending value, since it reaches logs and the API.
            f"{base}/{MANIFEST_FILENAME}": build_agent_manifest(
                name=name,
                description=agent.get("description") or "",
                slug=slug,
                handler_type=agent["handler_type"],
                config=config,
                oasf_skills=oasf_skills,
            ),
            # Differs on every approval, so each one commits a changed tree and the
            # build workflow (which builds the agent dirs a push changed) always
            # builds a new image, also for unchanged code and card (a reject then
            # approve, or a re-upload of the same files). The build poll waits for
            # an image newer than this approval.
            f"{base}/.approval": json.dumps(
                {
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                    "revision": tree_revision(handler_dir),
                },
                indent=2,
            ) + "\n",
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
            repo, token, files, f"Register custom agent: {name}", directory=base
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
    repo: str, token: str, files: dict[str, str], message: str, *, directory: str
) -> None:
    """Commit ``files`` as the whole content of ``directory``, in ONE commit via
    the Git Data API.

    A single atomic commit is important: the build workflow triggers on push to
    registered_agents/**, and committing files one at a time (Contents API) would
    fire the build before the Dockerfile exists. Tree entries use inline `content`,
    so no separate blob-creation round-trips are needed. A file an earlier commit
    put under ``directory`` that ``files`` does not name is deleted in the same
    commit, so the image is built from exactly these files (the build copies the
    whole directory).
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
        listing = (
            await client.get(f"{api}/git/trees/{base_tree_sha}", params={"recursive": "1"})
        ).raise_for_status().json()
        if listing.get("truncated"):
            raise RuntimeError(f"Cannot list {repo}'s tree to replace {directory}/: listing truncated")

        prefix = directory.rstrip("/") + "/"
        tree = [
            {"path": path, "mode": "100644", "type": "blob", "content": content}
            for path, content in files.items()
        ] + [
            {"path": it["path"], "mode": it["mode"], "type": "blob", "sha": None}
            for it in listing.get("tree", [])
            if it.get("type") == "blob" and it["path"].startswith(prefix) and it["path"] not in files
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


async def _image_digest_on_dockerhub(image_name: str, tag: str = "latest") -> str | None:
    """The manifest digest ('sha256:...') for {image_name}:{tag}, or None if
    unavailable. Persisted as image_ref so a future CloudRunBackend can deploy by
    digest instead of the floating ':latest' tag (reproducibility/rollback — the
    same footgun already hit with vLLM's cache path, see GCP_MIGRATION.md).
    """
    url = f"https://hub.docker.com/v2/repositories/{image_name}/tags/{tag}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        return resp.json().get("digest")
    except Exception as exc:
        logger.warning("dockerhub_digest_error image=%s error=%s", image_name, exc)
        return None


# ── Module-level singleton ─────────────────────────────────────────────────────

_supervisor: Supervisor | None = None


def get_supervisor() -> Supervisor:
    global _supervisor
    if _supervisor is None:
        if fake_supervisor_enabled():
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
    """Typed config for a stored row.

    Goes through ``parse_config``, which accepts BOTH the canonical shapes and the
    pre-consolidation ones — rows written before the taxonomy change still carry
    ``handler_type`` = ``api_proxy``/``tool_loop``/``endpoint_ops`` and the matching
    old config JSON, and are normalized here on read. Old rows are deliberately not
    rewritten in place; only newly registered agents are stored canonical.
    """
    handler_type: str = agent["handler_type"]
    raw = agent["config"]
    config_data: dict = raw if isinstance(raw, dict) else json.loads(raw)
    return parse_config(handler_type, config_data)


def _uploads_code(agent: dict[str, Any]) -> bool:
    """Whether a stored agent ships uploaded Python: the gate for admin approval,
    the per-agent image build, and (on the cluster) redeploy on startup recovery.

    Asks the CONFIG, not the handler_type name: both a ``custom`` handler and an
    ``llm`` agent whose ``tools.source`` is ``upload`` qualify, including rows
    stored under a legacy name (normalized by ``_deserialize_config``). A malformed
    stored config is treated as "not uploaded code" (the API then 404s rather than
    500s, and recovery does not re-queue it).
    """
    try:
        return carries_upload(_deserialize_config(agent))
    except Exception:
        return False


def _build_agent_card(
    agent_id: str,
    name: str,
    description: str,
    oasf_skills: list[dict[str, Any]],
    port: int,
    credentials: list[dict[str, Any]] | None = None,
    resources: dict[str, Any] | None = None,
) -> AgentCard:
    skills = [
        AgentSkill(
            id=s.get("key") or s["name"],   # prefer canonical key; fall back to name
            name=s["name"].split("/")[-1],
            description=description,
            # Self-describe the ADS routing skill via an 'oasf:<name>:<id>' tag
            # so the card is discoverable without a per-agent entry in
            # the converter's routing map (agentnet/directory/card_oasf.py).
            tags=(
                [oasf_skill_tag(s.get("key") or s["name"], s["id"])]
                if s.get("id") is not None
                else []
            ),
        )
        for s in oasf_skills
    ]
    # Declared credentials (P4a) and resource requirements (Step 8) ride on
    # capabilities.extensions — the only field the planner's discovery read path
    # round-trips. Each is omitted when empty, keeping other cards byte-identical.
    exts = [
        e for e in (
            build_credentials_extension(credentials),
            build_resources_extension(resources),
        ) if e is not None
    ]
    return AgentCard(
        name=name,
        description=description,
        url=f"http://localhost:{port}/",
        version="1.0.0",
        # streaming=True so the planner uses SSE and honors the executor's <30s
        # heartbeat (P3) — this is the card the planner actually reads from ADS.
        # Every handler the subprocess backend runs serves streaming too (their
        # own cards say so; test_streaming_agents checks they agree).
        capabilities=AgentCapabilities(
            streaming=True,
            extensions=exts or None,
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
