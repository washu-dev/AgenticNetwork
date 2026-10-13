# Implementation plan — `CloudRunBackend`

**2026-07-29**, progress updated **2026-07-30**. Concrete, ordered steps to build the `CloudRunBackend`
and its supporting changes. Design + rationale: [`DESIGN_CLOUDRUN_BACKEND.md`](DESIGN_CLOUDRUN_BACKEND.md)
(see its **Corrections (2026-07-30)** note before trusting the converter-divergence claim in "Current-state
facts"). Manual reference implementation (what this automates): the Phase 1-3 work now folded into
[`MIGRATION.md`](MIGRATION.md). Decisions resolved 2026-07-29 (see design doc).

Each step is independently testable and lands behind the existing `SPAWN_BACKEND` switch, so nothing
changes for the cluster/subprocess paths until `SPAWN_BACKEND=cloudrun`.

All file refs are relative to the repo root.

## Progress (2026-07-30)

**Steps 0-8 done; Step 9 core PROVEN LIVE on GCP (2026-08-03).** Baseline:
`FAKE_DB=1 PYTHONPATH=. registration_service/.venv/bin/python -m pytest registration_service/tests -q`
→ **189 passed, 1 skipped** (Step 8 +8; two live-debug regressions +2; merged `build_manifest` +38).
**Step 9 core:** the registration service runs on Cloud Run (`registration`, `SPAWN_BACKEND=cloudrun`) wired
to Cloud SQL + the VPC connector, and drove a custom agent end-to-end: register → CI build → `image_ready`
→ `CloudRunBackend` auto-deploy (`agent-echo-test`, min=0, as `agent-runtime`) → card published. First live
Phase-4 loop. Three bugs were found + fixed live (see Step 9 notes below).
**Steps 0-9 DONE** (Step 9 = reg service + planner both on Cloud Run/Cloud SQL, register→deploy loop proven
live). **Step 10 (planner front-door) + the Option-1 cutover are the next steps**, paused since the end of
summer 2026 at a stable milestone, not blocked. Cloud is a critical leg of the project going forward, so
resuming is a priority (2026-10-13). The design is **settled** and the build outline is in Step 10 below,
so a successor can start without re-deciding. The ENGR cluster stays the live public deployment meanwhile.
Handoff summary:
[`gcp/README.md`](README.md#status--deferred-work-handoff).

**Roadmap end-state.** Steps 5–8 finish the agent-onboarding automation; **Steps 9–10** (Cloud SQL
persistence + planner front-door hardening) make the planner safe to expose and are the prerequisites for
the migration's final move: pointing `agenticweb.engr.wustl.edu` at GCP via **Option 1** (repoint the
WashU ENGR gateway upstream). Decision + procedure: [`DOWNSTREAM_CONSIDERATIONS.md`](../DOWNSTREAM_CONSIDERATIONS.md) #4.
So when Steps 0–10 are done, the Option 1 cutover is a config change on the out-of-repo gateway → a live
GCP instance behind the existing hostname.

Line numbers below were refreshed 2026-07-30 to match the post-Step-3 tree; they will drift again now
that Step 4 has landed, so treat them as a starting point, not gospel — grep to confirm.

---

## Step 0 — Cleanup / small fixes (no behavior change) — ✅ Done (2026-07-30)
- **Delete** stale `uploaded_handlers/` at repo root (leftover; the live dir is
  `registration_service/uploaded_handlers`).
- **Fix** the `agents` table CHECK constraint to include `tool_loop` (`registration_service/db.py:20` —
  the constraint omits it though models/handlers support it). Latent bug; fix while here.
- **Test:** `FAKE_DB=1 PYTHONPATH=. pytest registration_service/tests`.

**What actually happened:** the `uploaded_handlers/` deletion was moot — it doesn't exist on disk (root
or under `registration_service/`) and is `.gitignore`'d, so nothing was ever committed there. The CHECK
constraint fix landed as planned (both the inline `CREATE TABLE` and the `ALTER TABLE ... ADD CONSTRAINT`
migration in `db.py`). Added `registration_service/tests/test_db_schema.py`, which reads `_SCHEMA_SQL`
directly and cross-checks it against `RegisterRequest`'s canonical `handler_type` `Literal` — no live
Postgres needed to catch this class of drift.

## Step 1 — Backend capability flags (refactor; unblocks a clean 3rd backend) — ✅ Done (2026-07-30)
- In `registration_service/backend.py`, add flags to `ContainerBackend` (ABC, `:124`) with defaults
  matching `SubprocessBackend`:
  `owns_build=False`, `needs_host_loop=False`, `supports_health_restart=True`, `publishes_decoupled=False`.
- Set on `ClusterBackend`: `owns_build=True, needs_host_loop=True, supports_health_restart=False`.
- Replace the **six `isinstance(self._backend, ClusterBackend)` sites** in `supervisor.py`
  (`:129, 256, 356, 414, 444, 454`) with the corresponding flag checks.
- **Test:** existing `registration_service/tests` (esp. `test_cluster_build.py`, `test_subprocess.py`)
  pass unchanged — this is a pure refactor.

**What actually happened:** landed exactly as planned. The six sites now read
`_backend_flag(self._backend, "<flag>", <default>)` (a small module-level getattr helper in
`supervisor.py`) rather than a plain attribute access, because `test_cluster_build.py`'s `_FakeBackend`
is duck-typed (doesn't subclass `ContainerBackend`) and would otherwise `AttributeError`. Flag mapping:
`:129`→`owns_build`, `:256/:414/:444/:454`→`needs_host_loop`, `:356`→`supports_health_restart` (inverted).
`ClusterBackend` import removed from `supervisor.py` (no longer referenced there). 115 passed, unchanged.

## Step 2 — Discoverability: enforce the OASF routing tag + fix the publish converter — ✅ Done (2026-07-30, narrower than planned)
- In `registration_service/oasf_skills.py` + the card-build path (`supervisor._build_agent_card`,
  `:713`), **guarantee ≥1 skill carries an `oasf:<name>:<id>` tag**; reject/repair a card that resolves
  to none (else it publishes stored-but-not-routed → invisible). Preflight this at register/approve.
- ~~Point the registration image's publish at the **working** skill-mapping logic: either swap
  `AgenticNetwork/ads_utils/converter.py` to the populated `agent-directory/ads_utils/converter.py`
  logic (populated `_AGENT_OASF_ROUTING_SKILLS` + `oasf:` tag parsing + hard-fail on empty), or vendor
  that copy into the registration image. Do **not** attempt full converter unification here.~~ **Already
  done before this pass** — see correction below. No action needed.
- **Test:** register a `custom` agent with an `oasf:` tag, run the publish path against a real ADS,
  assert `dirctl routing list` shows it with non-empty skills; register one *without* a resolvable tag,
  assert preflight rejects it.

**Correction — the converter-divergence bullet above (and in `DESIGN_CLOUDRUN_BACKEND.md`'s "Current-state
facts") describes an already-fixed state.** `ads_utils/converter.py` (the repo-root copy `registration_service`
actually imports and `Dockerfile.registration` bakes in — the only `ads_utils` in this repo) already has the
`oasf:<name>:<id>` tag mechanism and already hard-fails `_ads_routing_skills` on empty, since commit `0922ef5`
(2026-07-09) — three weeks *before* the design doc's 2026-07-29 draft. There was nothing to point at "the
working skill-mapping logic"; it already was. Verify with `git log --oneline -- ads_utils/converter.py`
before trusting this doc's framing of it again.

**What actually happened instead:** the real gap was that `OASFSkill.id` is optional (`int | None = None`)
with nothing enforcing it, so a skill list with no id anywhere would raise an uncaught `ValueError` deep
inside `DirectoryPublisher.publish()` — **after** the backend had already spawned a process/container (the
orphan/ghost failure mode this step exists to prevent, just triggered late instead of prevented early).
This was already latent in two test fixtures (`API_PROXY_PAYLOAD`, `ENDPOINT_OPS_PAYLOAD` both had
`"id": None`) that only passed because tests never configure a real `ADS_SERVER_ADDR`, so
`DirectoryUnavailable` short-circuited before the routing-skill check ever ran. Fixed: added
`oasf_skills.has_routable_skill()`, enforced at `server.register_agent` (→ `400`) and again in
`supervisor.spawn_and_publish` (defense-in-depth, before `self._backend.spawn(...)` runs). Fixed both
fixtures to carry a real taxonomy id. New tests in `test_api.py` and new `test_oasf_routing.py`.
Full converter unification remains correctly out of scope (per the design doc's own "targeted fix, not
unification" decision) — doubly so now since the targeted fix already existed.

## Step 3 — `image_ref` (digest) column — ✅ Done (2026-07-30)
- `registration_service/db.py`: add nullable `image_ref TEXT`; add it to the `update_agent` whitelist
  (`:143`, was `:138-146` — shifted by Step 0/2 edits); mirror in `fake_db.py`.
- `supervisor._check_building_agents` (`:422`, was `:397`) / `_image_exists_on_dockerhub` (`:676`, was
  `:644`): when the image is found, resolve and store the **digest** into `image_ref`.
- `CloudRunBackend.spawn` (Step 4) deploys `image_ref` (digest), not `:latest`.
- **Test:** unit-test the digest capture; assert `image_ref` populated at `image_ready`.

**What actually happened / contract for Step 4:** `image_ref` stores the **bare digest string**
(`"sha256:..."`), *not* a full `image@sha256:...` reference — per the design doc's "keep deriving the
image name from the slug" note, the name is still reconstructed the same way it already is elsewhere
(`f"{_image_registry()}/agenticnetwork-custom-{slug}"`). **`CloudRunBackend.spawn` must compose
`f"{image_name}@{image_ref}"` itself, and must fall back to `:latest` when `image_ref is None`**
(digest lookup can fail — best-effort, doesn't block the `image_ready` transition; also `None` for any
row that predates this column). Implementation: new `supervisor._image_digest_on_dockerhub(image_name,
tag="latest")` — a **second** Docker Hub GET, deliberately kept separate from `_image_exists_on_dockerhub`
rather than folded into it, because existing tests monkeypatch `_image_exists_on_dockerhub` directly as a
bare bool stub (used by both `_check_building_agents` and the cluster `recover_live_agents` reconcile
path) — changing its return shape would have broken three existing tests. `_check_building_agents` now
persists `status="image_ready"` + `image_ref` via `db.update_agent(...)` in one call (replacing the old
bare `_set_status(...)` call at that one site only — `_set_status` is still used everywhere else).
Also added a drift-guard test (`test_db_schema.py::test_updatable_columns_match_fake_db`) asserting
`db._UPDATABLE_COLUMNS == fake_db._ALLOWED_UPDATE_COLS`, two hand-kept whitelists with nothing else
checking they stay in sync. (Removed 2026-10-13: `fake_db` no longer keeps its own whitelist;
`db.update_agent` checks the columns before the fake runs.)

## Step 4 — `CloudRunBackend` (the core) — ✅ Done (2026-07-30, with the deploy trigger pulled forward)
- New class in `registration_service/backend.py` implementing `ContainerBackend`, flags:
  `owns_build=True, needs_host_loop=False, supports_health_restart=False, publishes_decoupled=True`.
  Use the **Cloud Run Admin API** (`google-cloud-run` client) — add to `requirements.txt`.
- `make_backend()` (`:385`, was `:373`): add `SPAWN_BACKEND == "cloudrun"` → `CloudRunBackend`.
- **`spawn(agent_id, name, description, handler_type, config, port)`** — precondition `image_ready`:
  create/replace Cloud Run service `agent-<slug>`:
  - image = `f"{image_name}@{image_ref}"` (fall back to `:latest` if `image_ref is None` — see Step 3's
    contract note above); container port 8080; `min-instances=0`,
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

**What actually happened:**
- `CloudRunBackend` landed in `registration_service/backend.py` with the four flags as planned, plus
  `make_backend()` gains the `"cloudrun"` branch. The `google-cloud-run` dependency is **lazily imported**
  (only inside `_CloudRunClient`, a thin wrapper), so `backend.py`, `make_backend()`, and the whole test
  suite import fine **without the package installed** — the 135 tests never touch google. Added
  `google-cloud-run>=0.10.0` to `requirements.txt` (not installed into the dev venv; only needed at
  runtime under `SPAWN_BACKEND=cloudrun`).
- **`spawn` gets the digest via `db.get_agent(agent_id)`** (the ABC signature has no `image_ref` param, so
  the backend reads it off the row itself) and composes `name@digest`, `:latest` when `None`. The
  image-composition + spec is a **pure `_deploy_spec()` helper**, unit-tested directly. `spawn` now
  returns `str | None` (the run.app URL); the ABC annotation was widened to match — the local/cluster
  backends still return `None`.
- **Port pinned to 8080.** `container_runner` binds the `AGENT_CONFIG_JSON` `port` (falling back to
  `AGENT_PORT`), **not** Cloud Run's `$PORT` — so `_deploy_spec` forces both the envelope port and the
  service container port to `_CLOUD_RUN_CONTAINER_PORT=8080`; the DB-allocated port is ignored off-host.
- **The deploy *trigger* was pulled forward from Step 5** (per the user's call). For cluster
  (`needs_host_loop=True`) the host srun loop launches `image_ready` agents; Cloud Run has no host loop,
  so `supervisor._check_building_agents` now calls a new **`_deploy_ready_agent(agent)`** inline when
  `owns_build and not needs_host_loop` — it runs `spawn`, then `_publish_and_set_live(...,
  endpoint_url=<run.app URL>)`. `_publish_and_set_live` gained an optional `endpoint_url` override
  (defaults to `localhost:port` for the local/cluster paths). This means the run.app card is **already
  advertised** end-to-end; Step 5's remaining work is the scale-to-zero *no-unpublish* behavior.
- **`_cluster_build_enabled()` was renamed to `_oob_build_enabled()`** and broadened to
  `SPAWN_BACKEND in ("cluster", "cloudrun")` — both share the GitHub→Docker Hub build; they diverge only
  at launch. (4 call sites + def; no test referenced the old name.)
- **Known gap → Step 6:** a process restart between the `image_ready` transition and a successful deploy
  strands the row at `image_ready` (the build poll only scans `building`). Documented inline in
  `_deploy_ready_agent`'s docstring; `recover_live_agents` reconcile in Step 6 closes it.
- New tests: `registration_service/tests/test_cloudrun_backend.py` (11 tests) — `_deploy_spec` digest /
  `:latest` fallback / env forwarding / RFC1035 service id; backend spawn-by-digest + invoker grant;
  `is_alive` reads Ready **without HTTP**; `stop` deletes; `make_backend` selects cloudrun without
  importing google; and the supervisor trigger (deploy + publish run.app URL, and `failed` on deploy
  error). Uses an injected fake Cloud Run client. **135 passed.**

## Step 5 — run.app URL as `endpoint_url` + decoupled publish — ✅ Done (2026-07-30, mostly subsumed by Step 4)
- On successful `spawn`, persist `endpoint_url = <run.app URL>` (not localhost:port).
- `supervisor._publish_and_set_live` (`:180`, was `:156`): use `advertise_url = endpoint_url` (already
  the shape — just feed it the run.app URL). With `publishes_decoupled=True`, publish once on deploy and
  **do not unpublish on scale-to-zero** — only on `stop_and_unpublish`.
- **Test:** assert the published card's `url` is the run.app URL; assert no unpublish on a
  scale-to-zero transition.

**What actually happened:** Step 4's pulled-forward deploy trigger already did the *production* half of
this step — `_deploy_ready_agent` persists `endpoint_url = <run.app URL>` and publishes with
`advertise_url = <run.app URL>`, exactly once, on deploy (`_publish_and_set_live`'s optional `endpoint_url`
override). The **no-unpublish-on-scale-to-zero** half needed **no new code**: it's structurally guaranteed
already — (a) the *only* `unpublish` call site in the supervisor is `stop_and_unpublish` (explicit
teardown; nothing unpublishes based on liveness), (b) the health loop early-returns for
`supports_health_restart=False`, so a scaled-to-zero agent is never probed/restarted, and (c)
`CloudRunBackend.is_alive` reports "exists & Ready" = alive even at 0 instances, so it never looks dead.
So Step 5 is a **test lock** on the decoupled-publish contract rather than a behavior change:
- Extended the deploy test to assert publish-once (`len(published) == 1`), `ads_cid` persisted, and zero
  unpublishes during deploy.
- `test_scale_to_zero_keeps_card_published` — a full `_check_all_live_agents` pass over a live cloud agent
  leaves status `live`, `ads_cid` intact, zero unpublishes, service not torn down.
- `test_stop_and_unpublish_is_the_only_teardown` — explicit stop is the sole path that both unpublishes
  the card and deletes the Cloud Run service.
- 137 passed (135 + 2). The `publishes_decoupled` flag still has **no code consumer** — that arrives in
  Step 6's recovery reconcile (a reconciled cloud agent must preserve its decoupled card, not
  unpublish/re-spawn). A flag defined ahead of its consumer, as documented.

## Step 6 — Health loop + recovery gating — ✅ Done (2026-07-30)
- `supervisor._check_all_live_agents` (`:379`, was `:356`): gate on `supports_health_restart` (already
  early-returns for Cluster, via the Step 1 flag) so Cloud Run is skipped (self-heals; probing cold-starts it).
- `recover_live_agents` (`:263`, was `:239`): for `needs_host_loop=False` cloud backend, **reconcile DB
  `live` rows against actual Cloud Run services** via the API rather than re-spawning blindly.
- Retire the planner's manual `HEALTH_PROBE_SKIP_AGENTS` stopgap once the backend owns liveness — or
  carry an `on_demand` marker on the card the planner probe reads. (Interim: keep the env.)
- **Test:** health loop does not probe a cloudrun agent; recovery reconciles instead of re-spawning.

**What actually happened:**
- **Health-loop skip** needed no new code — `_check_all_live_agents` already early-returns for
  `supports_health_restart=False` (the Step-1 flag; `CloudRunBackend` sets it False). Locked with
  `test_health_loop_never_probes_cloud_agent`, which asserts the fake client's `get()` (the Admin-API
  liveness probe) is called **zero** times during a health pass over a live cloud agent — so a min=0
  service is never cold-started by the probe.
- **Recovery reconcile** is the real change. `recover_live_agents` was a two-way split (subprocess+cloud
  re-spawn / cluster ghost-reconcile); it's now a **three-way dispatch**. A new **cloud branch keyed on
  `publishes_decoupled`** (checked first, since it's also `needs_host_loop=False`) calls new
  **`_recover_cloud_agents()`** instead of the blind re-spawn. That branch: for each `live` row, confirm
  the service via `is_alive` (Admin-API get, never HTTP) → leave it if Ready (the decoupled card persists
  on the separate ADS VM), else drop the stale card (best-effort unpublish) and redeploy via
  `_deploy_ready_agent`; for each `image_ready` row, deploy it — **which closes the Step-4 stranded-row
  gap** (a restart between image_ready and deploy). Reuses `list_agents_by_status` (SELECT * in both real
  db + fake_db), so no new SQL patterns.
- **`publishes_decoupled` now has its consumer** (promised in Step 5): it's the discriminator selecting
  the reconcile-not-respawn recovery path — semantically apt, since a decoupled-publish backend is exactly
  one whose agents + cards outlive the reg-service singleton.
- **Planner `HEALTH_PROBE_SKIP_AGENTS`:** left as the interim env stopgap per the plan — it lives in the
  separate `planner_agent` codebase, and the doc explicitly defers it ("Interim: keep the env"). Retiring
  it (or a card `on_demand` marker) is a follow-up, tracked below.
- New tests (4): health-loop-never-probes; recovery **confirms** a live agent via the API (no re-deploy,
  no republish); recovery **redeploys** when the service vanished (stale card dropped first); recovery
  **deploys a stranded `image_ready`** row. **141 passed** (137 + 4).

## Step 7 — IAM (one-time infra) — ✅ Done (2026-08-03, live on GCP)
- Create a scoped **`agent-runtime` SA** (Secret Manager read + Vertex/vLLM as needed; **not** Editor).
- Grant the **registration service's runtime SA**: `run.admin` (deploy + set invoker IAM),
  `iam.serviceAccountUser` on `agent-runtime` SA, Secret Manager access, and VPC egress to ADS.
- (These mirror the manual `run.admin`/`secretmanager.admin` grants from Phases 1-3.)

**What actually happened (run by the user; captured in [`scripts/gcp_iam_step7.sh`](../../../deployment/scripts/gcp_iam_step7.sh), idempotent):**
- Two SAs created: `agent-runtime@di2-agenticweb.iam.gserviceaccount.com` (agent runtime identity) and
  `registration-service@di2-agenticweb.iam.gserviceaccount.com` (the reg-service deployer).
- Grants applied: `registration-service` → `roles/run.admin` (project) **✓ confirmed**;
  `registration-service` → `roles/iam.serviceAccountUser` **on the `agent-runtime` SA** (SA-scoped, not
  project-wide); `agent-runtime` → `roles/aiplatform.user` (project, Vertex fallback).
- **IAM permission wall (the recurring gotcha):** the user's account had Editor + resource-admin roles
  (`run.admin`, `secretmanager.admin`, `iam.serviceAccountAdmin`, `cloudsql.admin`) but **not**
  `resourcemanager.projects.setIamPolicy` — so project-level `add-iam-policy-binding` failed until an
  Owner granted `roles/resourcemanager.projectIamAdmin` (time-boxed, per IT). SA creation + SA-level
  bindings worked under the existing roles. Lesson: the whole migration's project-level IAM needs
  projectIamAdmin/Owner; batch it inside one grant window (Steps 9–10 need more).
- **Deferred deliberately** (were in the original bullet, moved out): `roles/vpcaccess.user` on the
  reg-service SA — only needed if on-demand agents attach the VPC connector for VPC-internal egress;
  agents calling Vertex (a Google API) don't. `roles/secretmanager.secretAccessor` — granted **per-secret**
  in Step 9, when the reg service's secrets (`GITHUB_TOKEN`, DB creds) exist (least-privilege > project-wide).
- **`PLANNER_INVOKER_SA = 267355023116-compute@developer.gserviceaccount.com`** — the planner currently
  runs as the project **default Compute Engine SA** (Editor-carrying). It works as the invoker member, but
  the planner running *as* an over-privileged default SA is a hardening gap → follow-up: give the planner
  its own least-privilege runtime SA. (Tracked in "Out of scope / follow-ups".)
- Reg-service `CloudRunBackend` env for the eventual deploy (Step 9): `SPAWN_BACKEND=cloudrun`,
  `GCP_PROJECT=di2-agenticweb`, `GCP_REGION=us-central1`,
  `AGENT_RUNTIME_SA=agent-runtime@di2-agenticweb.iam.gserviceaccount.com`,
  `PLANNER_INVOKER_SA=267355023116-compute@developer.gserviceaccount.com`, `IMAGE_REGISTRY=di2washu`
  (`VPC_CONNECTOR` left unset for now — agents don't need VPC egress yet).

## Step 8 — Resource-requirements card extension (right-sizing + backend choice) — ✅ Done (2026-08-03)
- Define ext `agenticnetwork.dev/ext/resource-requirements` (cpu/mem/gpu/concurrency/max_instances);
  parse in the card path; persist alongside `config`.
- `CloudRunBackend.spawn` maps it to `--cpu/--memory/--concurrency/--max-instances`. `gpu`/strict
  isolation → route to a future `GKEBackend` (out of scope now; leave the selection seam).
- **Test:** a card with resource hints produces the expected deploy args.

**What actually happened:**
- **Model:** `models.ResourceRequirements` (cpu/memory/concurrency/max_instances/gpu) with cpu/memory
  format validators + `ge=1` on the counts; added `resources: ResourceRequirements | None` to
  `RegisterRequest`. Every default equals the CloudRunBackend's prior hardcoded behavior, so an agent that
  declares nothing deploys byte-identically to before.
- **Persistence:** a nullable `resources JSONB` column (mirrors the Step-3 `image_ref` pattern —
  `_SCHEMA_SQL` + `ADD COLUMN IF NOT EXISTS` + `fake_db` row/`fake_create_agent`). Set at registration via
  an optional `create_agent(resources=...)` param; `server.register_agent` passes it **on the uploaded-code
  path only** (the sole path that reaches a cloud deploy — the `owns_build` guard rejects `llm`/`api_proxy`
  on cloud backends, so the non-upload raw-INSERT path and its 6-arg `fake_db` branch were left untouched).
  Not added to the update whitelist (set once at register), so the `test_db_schema` drift guard is
  unaffected.
- **Backend:** `_deploy_spec` gained a None-safe `concurrency` (absent → leave Cloud Run's default; set →
  pin, e.g. 1 for per-request isolation) mapped onto `RevisionTemplate.max_instance_request_concurrency`;
  cpu/memory/max_instances were already read from the `resource` dict since Step 4. `CloudRunBackend.spawn`
  now reads `resources` off the row (same `get_agent` call as `image_ref`).
- **GKE seam:** `spawn` raises loudly on a `gpu: true` request ("Cloud Run cannot provide a GPU; no
  GKEBackend configured") rather than misdeploying. Per-agent Cloud-Run-vs-GKE *routing* stays out of scope
  (the service still picks one backend via `SPAWN_BACKEND`); this is just the fork + clear failure.
- **Card visibility:** new `card_resources.py` (`build_resources_extension`, mirrors `card_credentials.py`)
  surfaces the sizing on the published card's `capabilities.extensions`; `_build_agent_card` now attaches
  both the credentials and resources extensions (each omitted when empty). This is for planner/UI
  visibility — the **authoritative** copy the backend deploys from is the `resources` column, not the card.
- **Tests (8 new):** model validation; `_deploy_spec` maps hints / omits concurrency when unset; the card
  carries (or omits) the extension; `spawn` uses the hints; `spawn` rejects a GPU request (seam); and an
  end-to-end register→deploy→publish asserting resources persist, drive the deploy spec, and reach the
  card. **187 passed** (179 + 8), 1 skipped.
- **Out of scope (logged):** surfacing `resources` in `build_manifest.py`'s `agent.json` (it's a new
  top-level field, not in the handler `config` the manifest allowlist reads — no interaction, safe to defer).

---

## Go-live prerequisites (Steps 9–10) + the Option 1 cutover

Steps 0–8 make the *agent fleet* self-deploying. Steps 9–10 make the **planner itself** safe to expose
to real users on GCP, which is what unblocks the public front-door cutover. The migration's terminal
move — pointing `agenticweb.engr.wustl.edu` at GCP via **Option 1** (repoint the WashU ENGR gateway's
upstream to the GCP planner; keep the box, DNS, TLS, and Shibboleth SP registration unchanged) — is
decided and documented in [`DOWNSTREAM_CONSIDERATIONS.md`](../DOWNSTREAM_CONSIDERATIONS.md) #4. These two
steps are its prerequisites; the cutover itself is a config change on the out-of-repo gateway, done last.

## Step 9 — Cloud SQL persistence (planner + registration service)

**What actually happened (registration-service half — DONE + PROVEN LIVE 2026-08-03; planner half pending):**
- Provisioned Cloud SQL `agenticnetwork-db` (POSTGRES_15, db-f1-micro), db `registration`, user `regsvc`.
  Full asyncpg **socket** DSN (`postgresql://regsvc:…@/registration?host=/cloudsql/<conn>`) in Secret Manager
  `registration-db-dsn`; password reset to **hex** (URL-safe — base64 `/`/`+` break a URL DSN).
- Deployed the reg service on Cloud Run as `registration-service` SA: `SPAWN_BACKEND=cloudrun`, the Step-7
  SA/project env, `ADS_SERVER_ADDR=10.128.0.3:8888`, `SKIP_STARTUP_WAITS=1`, `--add-cloudsql-instances`,
  `--vpc-connector planner-connector`, secrets `registration-db-dsn`+`github-token`, `ADMIN_EMAILS`.
  `ingress=all`+`--no-allow-unauthenticated` (IAM-private, reach via `gcloud run services proxy` — NOT
  `ingress=internal`, which 404s the external proxy). Deployed one custom agent end-to-end.
- **`entrypoint-registration.sh` needed a Cloud Run branch** (working-tree change): honor `$PORT`, and
  `SKIP_STARTUP_WAITS=1` to skip the cluster-only localhost Postgres/ADS TCP waits (Cloud SQL is a socket,
  ADS is over the connector — neither is on localhost).
- **Three bugs found + fixed live** (working-tree changes): (1) `backend.py` — asyncpg returns `resources`
  JSONB as a **string**; `spawn` must `json.loads` it (fake_db returns a dict → Step-8 tests missed it →
  live deploy failed with `'str' object has no attribute 'get'`); (2) `supervisor.py` — recovery now retries
  `failed`+`image_ref` agents (a built agent that failed at the deploy step shouldn't strand); (3) the
  entrypoint above. +2 regression tests → 189 passed.
- **Cleanup — purge sparing (DONE):** the startup purge (`db.py` / `fake_db.init_fake_db`) now deletes only
  `status='stopped' OR (status='failed' AND image_ref IS NULL)` — it **spares `failed`+`image_ref` rows** so
  fix (2)'s recovery-retry actually fires (previously the purge deleted them first). Shared `_is_purgeable`
  helper in `fake_db` keeps the two fake handlers consistent; +1 test. 190 passed. (Needs a reg-image
  rebuild+redeploy to take effect on GCP — not urgent; the live agent is already deployed.)
- **Operational gotchas that made the live re-test painful** (not bugs — worth a runbook note): the
  painful (not bugs, but worth a runbook note): `_image_exists_on_dockerhub`'s **freshness guard** refuses an
  image older than the agent's `updated_at` (blocks reusing an image after re-register), and the
  `update_updated_at` **trigger** force-sets `updated_at=now()` (can't backdate a row — set `status`+`image_ref`
  directly + restart so recovery deploys it).
- **Planner → Cloud SQL (DONE 2026-08-03):** pointed the planner at the **same** `registration-db-dsn`
  secret + instance (the planner's `db.py` reads the same `REGISTRATION_DB_DSN` and shares the `registration`
  database — disjoint tables, distinct trigger fns `planner_update_updated_at` vs `update_updated_at`, no
  clobber). Granted the planner SA (default compute SA) `cloudsql.client` + accessor on the secret;
  `gcloud run services update planner --add-cloudsql-instances --update-secrets=REGISTRATION_DB_DSN=…` (additive,
  preserved its existing secrets). init_db came up clean. **Note:** setting the DSN also activates the planner's
  auth/credential routers (gated on the same env); `PLANNER_ALLOW_ANON=1` still allows anon, so no lockout —
  enforcement + `CREDENTIALS_ENCRYPTION_KEY` are Step 10. **Step 9 complete** (both services on Cloud SQL).

### Original plan
- Provision a **Cloud SQL Postgres** instance; wire both the planner and the registration service to it
  (Cloud SQL connector / auth proxy or private IP over the VPC), replacing the ephemeral/local Postgres.
  Both services need durable Postgres before real users: the planner for `users`, `user_credentials`,
  `conversation_messages`, `pending_confirmations`, `agent_credential_grants` (CLAUDE.md §8); the
  registration service for the `agents` table the CloudRunBackend lifecycle sits on (`db.py`). The reg
  service can't run the CloudRunBackend *live* on GCP without its DB, so this also unblocks deploying the
  registration singleton on Cloud Run.
- Run the existing schema migrations against Cloud SQL (`db.py` `init_db`); move DB creds to Secret
  Manager. No app-code change beyond connection config — both already use asyncpg pools.
- **Test:** planner + registration service come up against Cloud SQL; schema present; a registered agent
  row + a conversation row survive a service restart.
- **Gates:** DOWNSTREAM #4 prerequisite (1).

## Step 10 — Planner front-door hardening (Option 1 readiness) — ⏸ next step (design settled 2026-08-03)

> **Handoff note.** This step + the Option-1 cutover were deferred at end of summer 2026. The approach is
> decided and the alternatives are recorded, so whoever picks this up can start from the pre-checks
> without re-litigating the design. The repo holds only the outline below, not a full runbook.

**Decision (2026-08-03): the secure gateway→planner leg = Option A** (gateway mints a Google ID token →
planner run.app). Evaluated against Option B (external LB + Cloud Armor): A needs **no org-policy exception**
(the org blocks public `allUsers` Cloud Run; B's serverless-NEG→Cloud-Run path requires `allow-unauthenticated`,
so B is gated on an exception or IAP), keeps the existing WashU Shibboleth SP, adds **no GCP infra** (no LB/
cert), and puts the work on the gateway we control. IAP+WIF (Option D) is the clean long-term end-state but
needs org-level GCP access + WashU-IdP re-registration — deferred.

**Option A build outline (review before running; this outline is all the repo has):**
- *Pre-checks:* (1) is SA-key creation allowed? `gcloud org-policies describe
  constraints/iam.disableServiceAccountKeyCreation` — if enforced, use Workload Identity Federation instead
  of a key. (2) planner run.app URL (the token audience): `gcloud run services describe planner
  --format='value(status.url)'`.
- *GCP (tiny):* create SA `agenticweb-gateway@`; grant it `run.invoker` **on the planner only**; create a key.
  Planner stays `--no-allow-unauthenticated`/`ingress=all` (unchanged).
- *Gateway (the work):* deploy the key (chmod 600, dedicated user); a cron/systemd timer every ~30 min mints
  an ID token (audience = run.app URL, via `google-auth` `IDTokenCredentials`) into a file; the existing
  reverse-proxy config (nginx/Apache — whichever proxies to the ENGR planner today) is repointed to run.app
  **and** injects `Authorization: Bearer <token>`, sets SNI + `Host: <run.app host>`, keeps `proxy_buffering
  off` for `/messages/stream` SSE, and forwards the mod_shib identity headers.
- *The flip (together):* `gcloud run services update planner --remove-env-vars=PLANNER_ALLOW_ANON` + repoint
  the gateway upstream to run.app.
- *Validate first:* from the gateway, `curl -H "Authorization: Bearer $(cat token)" …/health` → 200; a bare
  curl (no token) → 403; confirm the refresh survives a token expiry.
- *Rollback:* flip the gateway upstream back to the ENGR planner (Slurm stays live).
- *Security must-dos:* the gateway MUST strip client-supplied `mail`/`eppn`/`remote-user` (test with a forged
  header); SA-key hygiene + rotation (or WIF); SA scoped to `run.invoker` on the planner only; monitor the
  token refresh (stale token = outage).
- *Open before starting:* pre-check #1 result (key vs WIF); which gateway layer proxies today (nginx/Apache);
  can the gateway run `google-auth`/`gcloud`.

### Original plan
- **Drop `PLANNER_ALLOW_ANON`** on the GCP planner so it enforces identity (no anonymous path).
- **Lock ingress** so the Shibboleth SP proxy is the *only* way in (internal-only / LB-only), and ensure
  the trusted proxy **strips any client-supplied `eppn`/`mail`/`remote-user` headers** before injecting
  its own — the planner blindly trusts them (`main_agent/auth.py`), so a direct-reachable planner is an
  auth bypass. This invariant *is* the cutover.
- **Wire the gateway→private-Cloud-Run leg:** the on-campus ENGR gateway must reach the private planner.
  Preferred cheapest path: external HTTPS LB + **Cloud Armor** allowlisting the gateway egress IP + a
  shared-secret header (or mTLS); alternative: a reverse autossh/wireguard relay VM mirroring `vllm-relay`
  (ENGR→GCP). Avoid VPN/Interconnect for now (IT-gated).
- **Validate on a scratch name first** (or via `gcloud run services proxy`): real SAML login → headers
  arriving → sessions/credentials working end-to-end — *before* touching the production hostname.
- **Test:** an unauthenticated direct hit to the planner is refused; a request carrying forged identity
  headers but not via the SP is rejected/stripped; a request through the SP path succeeds and the planner
  sees the right `mail`.
- **Gates:** DOWNSTREAM #4 prerequisite (2).

**Then — the Option 1 cutover (out-of-repo, done last):** repoint the WashU ENGR gateway's reverse-proxy
upstream from the Slurm node to the GCP planner's secure endpoint. Rollback = flip the upstream back
(Slurm stays a live backup). Procedure + rationale: DOWNSTREAM #4. **After this, `agenticweb.engr.wustl.edu`
serves the GCP instance.**

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
- **Retire the planner's `HEALTH_PROBE_SKIP_AGENTS` stopgap** (planner_agent codebase): now that the
  registration backend owns Cloud Run liveness (Step 6), the planner probe should skip on-demand agents
  via an `on_demand` card marker instead of the hand-maintained env list. Interim env kept for now.
- **Give the planner its own least-privilege runtime SA.** It currently runs as the project default
  Compute Engine SA (`267355023116-compute@…`, Editor-carrying) — over-privileged. Create a dedicated
  planner SA scoped to what it needs (Vertex, Secret Manager, its ADS/VPC path) and redeploy as that SA;
  update `PLANNER_INVOKER_SA` accordingly. (Surfaced during Step 7.)
