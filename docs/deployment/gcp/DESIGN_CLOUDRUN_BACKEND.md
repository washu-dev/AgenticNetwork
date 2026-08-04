# Design — `CloudRunBackend`: automate register → deploy on GCP

**Draft 2026-07-29.** Design for the Phase-4 automation: when a user registers/approves an agent, it's
**built, deployed to Cloud Run (scale-to-zero), and published to ADS automatically** — no manual
recipe. Grounded in a four-repo deep dive (registration_service, agent-directory, both agent-skeletons,
AgenticNetwork infra). Companion to `GCP_MIGRATION.md` (load-bearing change #2; its "Shipped —
Phases 0-3" section is the manual reference this automates). Implementation progress + step-by-step
corrections: [`IMPLEMENTATION_CLOUDRUN_BACKEND.md`](IMPLEMENTATION_CLOUDRUN_BACKEND.md) (Steps 0-3 done
as of 2026-07-30). **Read the "Corrections (2026-07-30)" note below before trusting the "Converter
divergence" and "six `isinstance`" bullets further down** — both describe states that implementation
work has since resolved or found to be already-stale.

---

## The one-paragraph thesis

The automation seam already exists: the registration service launches agents through a pluggable
**`ContainerBackend`** (`registration_service/backend.py:124` — `spawn`/`stop`/`is_alive`), selected by
`make_backend()` (`backend.py:385`, was `:373` pre-Step-1) on `SPAWN_BACKEND`. Today there's
`SubprocessBackend` (local) and
`ClusterBackend` (Slurm). **We add `CloudRunBackend`.** Crucially, the **build half is already done and
reusable as-is**: for uploaded-code agents the supervisor commits a build context to the
`agent-directory` repo → the `build-custom-agent.yml` GitHub Action builds `di2washu/agenticnetwork-
custom-<slug>:latest` (`linux/amd64`) → the supervisor polls Docker Hub for `image_ready`. That image
is already Cloud-Run-pullable. **Only the *launch* step is new**: instead of `srun`, call the Cloud Run
Admin API. So `CloudRunBackend` mirrors **`ClusterBackend`** (out-of-band build, then launch), not
`SubprocessBackend`.

---

## Current-state facts that shape the design

- **Backend contract** (`backend.py:124-140`): `async spawn(agent_id, name, description, handler_type,
  config, port)`, `async stop(agent_id)`, `async is_alive(agent_id)`. Backend-agnostic supervisor +
  `agents` table above it.
- **`ClusterBackend` is the template** (`backend.py:288`): its `spawn()` is a **no-op** — build/launch
  happen out-of-band (GitHub commit → Actions → Docker Hub; host loop `srun`s the ready image and
  calls back `finalize_spawned`). We replace only the launch mechanism.
- **Build pipeline (reuse unchanged):** `supervisor._commit_to_github` (`supervisor.py:474`) commits
  `registered_agents/<slug>/` = generated `Dockerfile` (ENTRYPOINT `python -m
  agent_skeleton.container_runner`), `<slug>.card.json`, user `src/`, vendored `agent_skeleton/`. GH
  Action triggers on `push` to `registered_agents/**`, builds+pushes to Docker Hub, **deploys nowhere**.
- **The container** (`agent_skeleton/container_runner.py`): reads `AGENT_CONFIG_JSON` env, branches on
  `handler_type`, serves A2A on `0.0.0.0:$PORT`, and **does not publish to ADS** — the supervisor does.
- **ADS publish (supervisor-owned):** `supervisor._publish_and_set_live` → `DirectoryPublisher().publish(card, advertise_url=endpoint_url)`
  (`ads_utils`), stores `backend_ref` as `ads_cid`; `stop_and_unpublish` unpublishes. Publish is
  best-effort (DirectoryUnavailable → warning).
- **Discoverability invariant:** ADS routes on the **routing index**; a record with empty top-level
  OASF `skills` is *stored but not routed* → invisible. Skills come from the converter's routing-skill
  list, whose reliable source is an **`oasf:<name>:<id>` tag on ≥1 card skill**. The planner builds
  `DiscoveredAgent` from the record's embedded `card_data`, not top-level skills — so empty skills
  breaks *routing/discovery*, not parsing.
- **Converter divergence (must resolve):** ~~the copy that populates skills correctly (oasf: tags +
  per-agent map, hard-fails on empty) is `agent-directory/ads_utils/converter.py`. The registration
  image bakes `AgenticNetwork/ads_utils/` whose `_AGENT_OASF_ROUTING_SKILLS` is **empty** and which can
  silently emit `skills: []`.~~ **Corrections (2026-07-30):** this description is stale. The repo-root
  `ads_utils/converter.py` (the *only* `ads_utils` in this repo — the one `registration_service` actually
  imports and `Dockerfile.registration` bakes in) already has the `oasf:<name>:<id>` tag mechanism and
  already hard-fails `_ads_routing_skills` on empty, since commit `0922ef5` (2026-07-09) — three weeks
  *before* this draft. `_AGENT_OASF_ROUTING_SKILLS` being empty there is by design (superseded by the tag
  mechanism), not a bug. There was nothing to "point at the working skill-mapping logic" — see
  `IMPLEMENTATION_CLOUDRUN_BACKEND.md`'s Step 2 for what the real gap turned out to be (the API layer
  didn't enforce a routable tag exists, so an unroutable card could get past registration and fail late,
  mid-spawn — now fixed) and what actually shipped. So a registered agent is only discoverable if its
  card carries an `oasf:` tag (which the OASF-skill selection at registration should stamp —
  `registration_service/oasf_skills.py`) — that part of the original bullet still holds.

**Hard-coded assumptions that break under Cloud Run** (from the dive):
- `endpoint_url`/card `url` = `http://localhost:{port}/` (`supervisor.py:191`, was `:167`) → must be the
  `*.run.app` URL.
- `is_alive` = HTTP GET `localhost:{port}/health` (`backend.py:262`, was `:254`, in `SubprocessBackend`) →
  an HTTP GET **cold-starts a min=0 service**; use the Cloud Run Admin API (service exists/ready)
  instead, don't wake it.
- Publish/unpublish coupled to process lifetime → fine for min=0 *if* published once with the stable
  run.app URL (the card persists at 0 instances; that's the decoupling).
- `port_pool` (`port_pool.py`) → irrelevant for external routing; keep a nominal port for the container
  to bind `$PORT`, ignore it for addressing.
- ~~**Six `isinstance(self._backend, ClusterBackend)` branches** in the supervisor (`supervisor.py:129,
  256, 356, 414, 444, 454`) → refactor into `ContainerBackend` capability flags or `CloudRunBackend`
  trips the subprocess branches.~~ **Done (2026-07-30, Step 1).** All six sites now read a capability flag
  (`owns_build`/`needs_host_loop`/`supports_health_restart`) via a getattr helper instead of `isinstance`;
  `CloudRunBackend` just needs to set the right flags, no subprocess-branch tripping risk. See
  `IMPLEMENTATION_CLOUDRUN_BACKEND.md` Step 1 for the exact flag-to-site mapping.

---

## Design

### 1. Slot-in
`make_backend()` gains a `"cloudrun"` branch returning `CloudRunBackend`. Supervisor + DB unchanged.

### 2. Refactor the `ClusterBackend` special-casing into capability flags (do this first)
Add to `ContainerBackend` (defaults matching `SubprocessBackend`):
- `owns_build: bool` — build happens out-of-band (commit→CI), spawn is a no-op until `image_ready`.
- `needs_host_loop: bool` — cluster uses an external srun loop; Cloud Run does **not** (it calls the API directly).
- `supports_health_restart: bool` — whether the supervisor health loop should restart it (False for
  Cloud Run — it self-heals).
- `publishes_decoupled: bool` — publish once with a stable URL, don't unpublish on scale-down.

Replace the six `isinstance` sites with these flags. `ClusterBackend` sets `owns_build=True,
needs_host_loop=True, supports_health_restart=False`. `CloudRunBackend` sets `owns_build=True,
needs_host_loop=False, supports_health_restart=False, publishes_decoupled=True`.

### 3. `CloudRunBackend` methods (via the Cloud Run Admin API — `google-cloud-run` client)
- **`spawn(agent_id, …, config, port)`** — precondition: `image_ready` (image on Docker Hub). Create/
  replace a Cloud Run service `agent-<slug>` for `di2washu/agenticnetwork-custom-<slug>:latest` with:
  `--min-instances=0 --max-instances=<from resource hints or 1>`, container port 8080, env
  `AGENT_CONFIG_JSON` (same envelope container_runner reads) + LLM env, `--no-allow-unauthenticated`,
  internal ingress, CPU/mem from resource hints. Then grant the **planner's runtime SA `run.invoker`**
  on the service. Return the `*.run.app` URL. (Because build is out-of-band, `spawn` for a not-yet-built
  agent is a no-op like Cluster; the deploy fires when `image_ready`.)
- **`stop(agent_id)`** — delete the Cloud Run service (supervisor unpublishes the card separately).
- **`is_alive(agent_id)`** — Cloud Run Admin API *get service* + check Ready condition. **Never HTTP-GET
  it** (would cold-start). Treat "service exists & Ready (even at 0 instances)" as alive.

### 4. Endpoint URL + decoupled publish
- Persist the run.app URL: extend `db.update_agent`'s whitelist (`db.py:138`) and write `endpoint_url`
  = the run.app URL (not localhost). (Optionally add a `service_name`/`image_ref` column; today image
  is derived from the slug and there's no image column.)
- Supervisor publishes with `advertise_url = <run.app URL>` (already the code path — just feed it the
  right URL). With `publishes_decoupled=True`, publish once on deploy; **do not unpublish on
  scale-to-zero** (only on `stop`). The card persists at 0 instances → planner can discover + cold-start.

### 5. Make published records routable (converter fix)
Ensure the supervisor's publish yields non-empty OASF `skills`. Two parts:
- **Guarantee an `oasf:<name>:<id>` tag** on ≥1 skill of every registered agent's card (the OASF-skill
  selection at registration — `oasf_skills.py` — should already do this; verify and enforce, else the
  agent is undiscoverable).
- **Unify the converter:** point the registration image's `ads_utils` at the canonical
  `agent-directory/ads_utils/converter.py` lineage (populates skills, hard-fails on empty) instead of
  the empty-map `AgenticNetwork/ads_utils` copy — or merge them. Track as a prerequisite bug (it's the
  same divergence that made `publish_agents.py` emit empty skills in the manual Phase-2 work).

### 6. Health loop + recovery
- Health loop (`supervisor._check_all_live_agents`, `:356`) already early-returns for Cluster; gate on
  `supports_health_restart` so it also skips Cloud Run (Cloud Run self-heals; probing would cold-start).
- **This subsumes the manual `HEALTH_PROBE_SKIP_AGENTS` hack** in the planner — with the backend owning
  liveness, the *planner's* health probe should also skip on-demand agents (carry an `on_demand` marker
  on the card, or keep the env stopgap short-term).
- Recovery (`recover_live_agents`, `:239`): for Cloud Run, **reconcile DB `live` rows against actual
  Cloud Run services** via the API (don't re-spawn blindly; the singleton's in-memory state is lost on
  restart).

### 7. Resource-requirements card field (load-bearing change #3)
Add a card extension (`agenticnetwork.dev/ext/resource-requirements`: cpu/mem/gpu/concurrency) → the
backend maps it to Cloud Run `--cpu/--memory/--concurrency/--max-instances`, and it's the signal for
**Cloud Run vs GKE** (GPU/strict-isolation → a future `GKEBackend`). Persist alongside `config`.

### 8. IAM — the registration singleton becomes a deployer
Its runtime SA needs: `run.admin` (create/delete services **and** set the invoker IAM binding),
`iam.serviceAccountUser` (to deploy as the agents' runtime SA if dedicated), Secret Manager access for
agent secrets, and **VPC egress to ADS** (the reg service already reaches ADS to publish). This mirrors
the manual grants we hit (`run.admin`, `secretmanager.admin`).

---

## Deploy model: service-driven, not CI-driven
- **Chosen:** the `CloudRunBackend` (in the registration service) calls the Cloud Run Admin API. It's
  on the VPC (can reach private ADS), owns the lifecycle/`agents` table, and integrates approval +
  on-demand + teardown.
- **Rejected as primary:** a GitHub Actions deploy job. It can't reach the private ADS to publish, and
  it's detached from the lifecycle. (Viable only for always-on first-party specialists, not the
  registration-driven user agents.)
- **Build stays in CI** (`build-custom-agent.yml`) unchanged — it produces the Cloud-Run-ready image.

## What to reuse vs build
- **Reuse unchanged:** the `agents` table + lifecycle, `_commit_to_github` build path + `builder.py` +
  vendored `agent_skeleton`/`container_runner.py`, the Docker Hub `image_ready` poll, supervisor
  publish/unpublish, `DirectoryPublisher`.
- **Build new:** `CloudRunBackend` (Cloud Run Admin API), the capability-flag refactor (✅ done), run.app
  URL persistence, `oasf:` tag enforcement (**not** converter unification — see "Resolved decisions"
  below), resource-field mapping, IAM for the reg SA, Cloud Run recovery.

## Rollout order (incremental, each testable)

Live status (which of these are actually done) lives in
[`IMPLEMENTATION_CLOUDRUN_BACKEND.md`](IMPLEMENTATION_CLOUDRUN_BACKEND.md), not here — this list is the
original plan and isn't kept in sync step-by-step.
1. Capability-flag refactor (no behavior change; unblocks a clean CloudRunBackend).
2. `CloudRunBackend.spawn/stop/is_alive` against the Cloud Run Admin API; wire `make_backend`.
3. run.app URL → `endpoint_url` + decoupled publish (advertise the run.app URL).
4. Converter unification + enforce `oasf:` tag (routability).
5. Reg-service SA IAM.
6. Health-loop/recovery gating (`supports_health_restart`, API reconcile) + retire the planner probe hack.
7. Resource-requirements card field → sizing + Cloud Run/GKE selection.
Prove with **one registered `custom` agent** end-to-end (register → approve → CI build → auto-deploy
min=0 → auto-publish → planner discovers → cold-start on query), then generalize.

## Resolved decisions (2026-07-29)
- **Converter — targeted fix, not unification (this pass).** Don't merge the divergent copies now
  (cross-repo packaging friction: `agent-directory` is a separate repo → needs a shared package/submodule).
  Instead: **enforce an `oasf:<name>:<id>` tag on ≥1 skill of every registered card** (via
  `registration_service/oasf_skills.py`) — all four converter copies honor that tag. (The reg-service
  publish path was *already* on the working skill-mapping logic as of 2026-07-09 — see the
  "Corrections" note near the top of this doc; there was no pointing to do.) Full converter unification +
  the 5-copy protocol dedup (CLAUDE.md §9) are tracked as separate follow-ups.
- **Agents run as one shared, low-privilege `agent-runtime` SA** — NOT the Editor-carrying default
  compute SA, and NOT per-agent SAs (defer that to the security-hardening phase). The registration SA
  deploys agents *as* this SA (`iam.serviceAccountUser` on it). Least-privilege without per-agent mgmt.
- **Persist `image_ref` (digest) and deploy by digest.** Add a nullable `image_ref` column; record the
  digest at `image_ready`; deploy `…@sha256:…` for reproducibility/rollback (avoids the `:latest`
  footgun we already hit with vLLM). Keep deriving the image *name* from the slug.
- **Opportunistic cleanup:** delete the stale top-level `uploaded_handlers/` leftover (unused; live one
  is `registration_service/uploaded_handlers`).

## Still open
- **First-party specialists** (email/geospatial/coding) still need their per-agent LLM work
  (Responses→Chat, vision/grounding, sandboxing) — orthogonal to this backend.
- **Cold-start UX**: the planner's `A2A_HTTP_TIMEOUT_SECONDS` must absorb Cloud Run cold start (set in
  Phase 3); confirm it's adequate for heavier agent images.
- **Per-agent SA graduation** — revisit shared→per-agent SA when isolation matters.
