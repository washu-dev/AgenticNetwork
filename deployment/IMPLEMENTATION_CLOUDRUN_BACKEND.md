# Implementation plan — `CloudRunBackend`

**2026-07-29.** Concrete, ordered steps to build the `CloudRunBackend` and its supporting changes.
Design + rationale: [`DESIGN_CLOUDRUN_BACKEND.md`](DESIGN_CLOUDRUN_BACKEND.md). Manual reference
implementation (what this automates): the Phase 1-3 work now folded into
[`GCP_MIGRATION.md`](GCP_MIGRATION.md). Decisions resolved 2026-07-29 (see design doc).

Each step is independently testable and lands behind the existing `SPAWN_BACKEND` switch, so nothing
changes for the cluster/subprocess paths until `SPAWN_BACKEND=cloudrun`.

All file refs are under `/Users/amaankhan/Desktop/DTRC/AgenticNetwork/`.

---

## Step 0 — Cleanup / small fixes (no behavior change)
- **Delete** stale `uploaded_handlers/` at repo root (leftover; the live dir is
  `registration_service/uploaded_handlers`).
- **Fix** the `agents` table CHECK constraint to include `tool_loop` (`registration_service/db.py:20` —
  the constraint omits it though models/handlers support it). Latent bug; fix while here.
- **Test:** `FAKE_DB=1 PYTHONPATH=. pytest registration_service/tests`.

## Step 1 — Backend capability flags (refactor; unblocks a clean 3rd backend)
- In `registration_service/backend.py`, add flags to `ContainerBackend` (ABC, `:124`) with defaults
  matching `SubprocessBackend`:
  `owns_build=False`, `needs_host_loop=False`, `supports_health_restart=True`, `publishes_decoupled=False`.
- Set on `ClusterBackend`: `owns_build=True, needs_host_loop=True, supports_health_restart=False`.
- Replace the **six `isinstance(self._backend, ClusterBackend)` sites** in `supervisor.py`
  (`:129, 256, 356, 414, 444, 454`) with the corresponding flag checks.
- **Test:** existing `registration_service/tests` (esp. `test_cluster_build.py`, `test_subprocess.py`)
  pass unchanged — this is a pure refactor.

## Step 2 — Discoverability: enforce the OASF routing tag + fix the publish converter
- In `registration_service/oasf_skills.py` + the card-build path (`supervisor._build_agent_card`,
  `:713`), **guarantee ≥1 skill carries an `oasf:<name>:<id>` tag**; reject/repair a card that resolves
  to none (else it publishes stored-but-not-routed → invisible). Preflight this at register/approve.
- Point the registration image's publish at the **working** skill-mapping logic: either swap
  `AgenticNetwork/ads_utils/converter.py` to the populated `agent-directory/ads_utils/converter.py`
  logic (populated `_AGENT_OASF_ROUTING_SKILLS` + `oasf:` tag parsing + hard-fail on empty), or vendor
  that copy into the registration image. Do **not** attempt full converter unification here.
- **Test:** register a `custom` agent with an `oasf:` tag, run the publish path against a real ADS,
  assert `dirctl routing list` shows it with non-empty skills; register one *without* a resolvable tag,
  assert preflight rejects it.

## Step 3 — `image_ref` (digest) column
- `registration_service/db.py`: add nullable `image_ref TEXT`; add it to the `update_agent` whitelist
  (`:138-146`); mirror in `fake_db.py`.
- `supervisor._check_building_agents` (`:397`) / `_image_exists_on_dockerhub` (`:644`): when the image
  is found, resolve and store the **digest** (`…@sha256:…`) into `image_ref`.
- `CloudRunBackend.spawn` (Step 4) deploys `image_ref` (digest), not `:latest`.
- **Test:** unit-test the digest capture; assert `image_ref` populated at `image_ready`.

## Step 4 — `CloudRunBackend` (the core)
- New class in `registration_service/backend.py` implementing `ContainerBackend`, flags:
  `owns_build=True, needs_host_loop=False, supports_health_restart=False, publishes_decoupled=True`.
  Use the **Cloud Run Admin API** (`google-cloud-run` client) — add to `requirements.txt`.
- `make_backend()` (`:373`): add `SPAWN_BACKEND == "cloudrun"` → `CloudRunBackend`.
- **`spawn(agent_id, name, description, handler_type, config, port)`** — precondition `image_ready`:
  create/replace Cloud Run service `agent-<slug>`:
  - image = `image_ref` (digest); container port 8080; `min-instances=0`,
    `max-instances`/`cpu`/`memory`/`concurrency` from the resource-requirements card ext (Step 8, else
    defaults); ingress internal; `--no-allow-unauthenticated`.
  - env: `AGENT_CONFIG_JSON` (same envelope `container_runner.py` reads) + LLM env + `ADS_SERVER_ADDR` if needed.
  - run as the **`agent-runtime` SA**.
  - grant the **planner runtime SA `run.invoker`** on the service (per-service IAM binding).
  - return the `*.run.app` URL.
- **`stop(agent_id)`** — delete the Cloud Run service (card unpublished by supervisor).
- **`is_alive(agent_id)`** — Cloud Run Admin API *get service* + Ready condition. **Never HTTP-GET**
  (would cold-start a min=0 service). "Exists & Ready (even at 0 instances)" = alive.
- Config/env: `SPAWN_BACKEND=cloudrun`, `GCP_PROJECT`, `GCP_REGION`, `AGENT_RUNTIME_SA`,
  `PLANNER_INVOKER_SA`, `IMAGE_REGISTRY`, `VPC_CONNECTOR`.
- **Test:** unit-test spawn/stop/is_alive against a mocked Cloud Run client; assert deploy args
  (min=0, digest, SA, invoker binding).

## Step 5 — run.app URL as `endpoint_url` + decoupled publish
- On successful `spawn`, persist `endpoint_url = <run.app URL>` (not localhost:port).
- `supervisor._publish_and_set_live` (`:156`): use `advertise_url = endpoint_url` (already the shape —
  just feed it the run.app URL). With `publishes_decoupled=True`, publish once on deploy and **do not
  unpublish on scale-to-zero** — only on `stop_and_unpublish`.
- **Test:** assert the published card's `url` is the run.app URL; assert no unpublish on a
  scale-to-zero transition.

## Step 6 — Health loop + recovery gating
- `supervisor._check_all_live_agents` (`:356`): gate on `supports_health_restart` (already early-returns
  for Cluster) so Cloud Run is skipped (self-heals; probing cold-starts it).
- `recover_live_agents` (`:239`): for `needs_host_loop=False` cloud backend, **reconcile DB `live` rows
  against actual Cloud Run services** via the API rather than re-spawning blindly.
- Retire the planner's manual `HEALTH_PROBE_SKIP_AGENTS` stopgap once the backend owns liveness — or
  carry an `on_demand` marker on the card the planner probe reads. (Interim: keep the env.)
- **Test:** health loop does not probe a cloudrun agent; recovery reconciles instead of re-spawning.

## Step 7 — IAM (one-time infra)
- Create a scoped **`agent-runtime` SA** (Secret Manager read + Vertex/vLLM as needed; **not** Editor).
- Grant the **registration service's runtime SA**: `run.admin` (deploy + set invoker IAM),
  `iam.serviceAccountUser` on `agent-runtime` SA, Secret Manager access, and VPC egress to ADS.
- (These mirror the manual `run.admin`/`secretmanager.admin` grants from Phases 1-3.)

## Step 8 — Resource-requirements card extension (right-sizing + backend choice)
- Define ext `agenticnetwork.dev/ext/resource-requirements` (cpu/mem/gpu/concurrency/max_instances);
  parse in the card path; persist alongside `config`.
- `CloudRunBackend.spawn` maps it to `--cpu/--memory/--concurrency/--max-instances`. `gpu`/strict
  isolation → route to a future `GKEBackend` (out of scope now; leave the selection seam).
- **Test:** a card with resource hints produces the expected deploy args.

---

## End-to-end acceptance (do after Steps 1-7)
Prove with **one registered `custom` agent**, `SPAWN_BACKEND=cloudrun`:
register → upload → approve → CI builds image → `image_ready` (digest recorded) → `CloudRunBackend`
deploys at `min=0` as `agent-runtime` SA + grants planner invoker → supervisor publishes the run.app
card (routable, `oasf:` tag) → planner discovers it → a query cold-starts it → answer → scales to zero.
Then generalize to the fleet.

## Out of scope / follow-ups (tracked, not this pass)
- Full converter unification + the 5-copy protocol dedup (CLAUDE.md §9); the two-`agent_skeleton`
  submodule collapse; scout's separate directory reconciliation.
- Per-agent SAs (graduate from the shared `agent-runtime` SA).
- `GKEBackend` for GPU/isolation agents.
- First-party specialists' LLM rewrites (email/geospatial/coding).
