# Multi-Contributor Workflow — Problem & Plan

> **Names (2026-10-25):** the chat app's deployment pieces this record calls `planner` (Cloud Run
> service, image `agenticnetwork-planner`, `PLANNER_SA`/`PLANNER_INVOKER_SA`, `planner-connector`,
> Slurm step and `planner.log`) are now named `chat-app` (`CHAT_APP_SA`, `CHAT_APP_INVOKER_SA`,
> `chat-app-connector`). This record keeps the names as they were deployed.

> **Status: validated on ENGR; merging to `main`.** The core is implemented and exercised on the
> cluster: group registry (`di2washu`) live, prod baseline healthy, prod/test instance modes with
> offset ports + node-local test Postgres, the submit-time deploy lock refusing duplicates, and
> ADS gossipsub isolation verified clean (test directory shows only test agents). Remaining work is
> CI (⏳ — build/push half unblocked by the registry; deploy half hinges on runner reach). Once
> this merges, promote the durable decisions into `DECISIONS.md` and link back here.
>
> **Update 2026-10-13:** an offline test CI (`.github/workflows/tests.yml`: unit suites, repo
> hygiene, native full-chain pipeline) is on `release-prep/full-chain`; the Phase 3
> build/push-on-merge and deploy halves are still open.

## The problem, precisely

The build + deploy + test workflow models exactly **one thing: a single shared production
instance, operated by one person.** There is no separate notion of "a contributor's own
instance." So the workflow breaks for a team in two distinct ways:

1. **Identity lock-in** — parts of the critical path are tied to *one person's* accounts, so
   another contributor can't build, deploy, or operate without going through them.
2. **Shared mutable state** — the one instance's Postgres volume, ports, ADS, and `.env` are
   singletons with no coordination, so a second contributor's run collides with (or corrupts)
   the first's.

The intended future is **not** N parallel production stacks. It is: **one canonical shared
instance that belongs to the *group*, not a person — plus a safe, isolated lane for
contributors to test changes before they reach it.**

## Guiding principle: the Slurm account is the model

The Slurm allocation (`-A engr-lab-di2`, `-p condo-di2` in `deployment/submit.sbatch` and
`start_vllm.sbatch`) is already **group-owned** — nobody thinks of it as any one person's, and
it works for the whole team. The coupling problem isn't that shared identity exists; it's that
*some* identities are group-owned (Slurm ✅) and others are still *personal* (❌). **The fix is
to make the personal ones group-owned, exactly like Slurm already is.**

## What's already fine (do not touch)

- **Slurm account/partition** — group-owned. This is the template, not a problem.
- **Google OAuth _client_** (`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_REDIRECT_URI`,
  `auth_router.py`) — the "agentic-web" app lives in a **shared (non-personal) Google Cloud
  project**. Confirmed group-owned. Leave it.
- **Per-user Google tokens** — the access/refresh tokens for each user's connected Google
  account are per-user by design, AES-256-GCM encrypted in `user_credentials`. Correct as-is.
- **`ADMIN_EMAILS`** — already a comma-separated allowlist (see DECISIONS.md D8). Correct.

## Coupling inventory (the evidence)

### A. Identity lock-in — still personal, should be group-owned

| What | Where | Why it blocks a contributor |
|---|---|---|
| DockerHub namespace `amaankhan25` — **build script** | `deployment/build_and_push.sh:17` (`REGISTRY=amaankhan25`) | A second contributor can't push without creds for that personal account. |
| DockerHub namespace — **submit** | `deployment/submit.sbatch` (10× `--container-image=amaankhan25/agenticnetwork-*`) | The cluster only ever pulls from the personal account. |
| DockerHub namespace — **application code** ⚠️ | `registration_service/backend.py:96`, `registration_service/supervisor.py:263,368` (`amaankhan25/agenticnetwork-custom-*`) | The trap: a contributor's *running* instance builds/pulls custom-agent images into the personal account. Not an env knob — it's a code literal. |
| GitHub write identity — **personal PAT** ⏳ | `registration_service/supervisor.py` (`GITHUB_TOKEN` in `_github_commit_files`/`_delete_from_github`; no `author`/`committer` set) + `deployment/secrets.env` | Every `registered_agents/…` commit is authored by the PAT owner, and the triggered "Build custom agent image" Actions run (`agent-directory/.github/workflows/build-custom-agent.yml`) lists them as actor — so the whole custom-agent build/push flow reads as one person. Plan: replace with a GitHub App (see Future work). |
| Image tags — `:latest` everywhere (+ ADS manually pinned `:v4`) | `deployment/submit.sbatch`, `docs/deployment/engr/README.md` | Any two builds clobber each other; ADS tag drifts and must be hand-bumped. |

### B. Shared mutable state — singletons with no coordination

| What | Where | Failure mode |
|---|---|---|
| **One Postgres data volume** | `deployment/submit.sbatch` (mount) + `deployment/entrypoints/entrypoint-postgres.sh:6-7` | Holds **real encrypted user credentials** (see below). `--dependency=singleton` is **per-user**, so two *different* contributors submitting double-mount the volume: the PID guard hard-fails the second job, and `DROP DATABASE dir` on startup wipes ADS state. |
| **One shared `deployment/.env`** on `/engrfs` | `deployment/submit.sbatch` sources it | Last editor wins; `OPENAI_BASE_URL` (ephemeral vLLM node) churns; secrets sit in plaintext readable by everyone with repo access. |
| **Fixed ports** (8090/8091/8888/9103/19105-8) | `deployment/submit.sbatch`, entrypoints | Bind unconditionally → same-node concurrency fails. The "5-digit ports avoid collisions" note is aspirational — they're hardcoded constants, not per-run-derived. |
| **Shared `registration` DB + port pool** | `registration_service/port_pool.py:11-12` (19200-19299) | Two contributors on one Postgres share one `agents` table and one port pool → `UNIQUE(port)` violations, cross-contaminated `/agents` lists. |

### The hard constraint: prod Postgres holds real secrets

The chat app writes to the **`registration`** database (`chat_app/store/db.py::_dsn`,
`DATABASE_URL`), which is the **persistent** DB on the shared `/engrfs` volume. It
contains **AES-256-GCM-encrypted Google OAuth refresh tokens**, users' OpenAI/Anthropic/Brave/
Google API keys (`user_credentials`), credential grants, and chat history. Therefore:

> **A contributor's test run must be provably incapable of touching the prod Postgres.** This
> means a *physically separate* test database instance — not just a different DB name on the
> same server.

## Proposed plan

Sequenced around the fact that **there was no CI** when this was written (2026-07; an offline test
workflow, `.github/workflows/tests.yml`, has existed since 2026-10). Each phase stands alone and delivers value without the next.

### Phase 0 — Group registry — ✅ done

The original coupling (evidence in the inventory above) was the personal `amaankhan25` DockerHub
account hardcoded across the build script, submit job, and application code.

- ✅ Every reference reads a single `IMAGE_REGISTRY` variable across **all three layers**:
  `deployment/build_and_push.sh`, `deployment/submit.sbatch` (all 10 `--container-image` refs via
  the `image_ref` resolver), and the code literals in `registration_service/backend.py`
  (`_image_registry()` helper) + `supervisor.py` (imports it). Also `IMAGE_TAG` / per-service
  `<SVC>_IMAGE_TAG` for candidate images. Documented in `deployment/.env.example` + README.
- ✅ **Committed default flipped to the group registry `di2washu`.** All 10 images
  (infra `:latest` + `ads:v4` + the 6 specialists) were pushed there first, then the default was
  swapped — so a fresh checkout deploys from `di2washu` with no override, and the personal account
  is out of the path. Override still available via `IMAGE_REGISTRY` for a personal build.
  - Note: `di2washu` is a shared DockerHub **user** account (whoever holds its creds can push); a
    DockerHub **org** would be cleaner group ownership later. If its repos are private, the cluster
    node needs pull credentials.

### Phase 1 — De-personalize the shared instance's config

- ✅ **vLLM endpoint auto-discovery** — `submit.sbatch` finds the running vLLM job (by the
  stable job name `agenticnetwork-vllm`, set in `start_vllm.sbatch`) and builds
  `OPENAI_BASE_URL` from its node, instead of a human hand-editing the shared `.env` each run.
  Prefers your own vLLM job, else any user's (a test instance can share prod's vLLM); falls
  back to the `.env` value if none is running; `VLLM_AUTODISCOVER=0` opts out. Port via
  `VLLM_PORT` (default 8100), kept in sync on both sides.
- ✅ **Secrets out of the shared `.env`** — the three real secrets
  (`CREDENTIALS_ENCRYPTION_KEY`, `GITHUB_TOKEN`, `GOOGLE_CLIENT_SECRET`) no longer live in the
  world-readable `deployment/.env`. They move to a restricted **secrets file** that
  `submit.sbatch` sources after `.env` (so it overrides): prod reads a shared operator-owned
  `<repo>/deployment/secrets.env` (group-readable, not world — keeps
  `CREDENTIALS_ENCRYPTION_KEY` stable), test reads a per-user `~/.config/agenticnetwork/secrets.env`;
  `SECRETS_FILE` overrides the path. Template + setup: `deployment/secrets.env.example`;
  gitignored via `secrets.env`. Non-secrets (`GOOGLE_CLIENT_ID`, `ADMIN_EMAILS`, dummy
  `OPENAI_API_KEY`) stay in `.env`.
  - ⏳ Follow-up: a real secret manager is the durable target (a group-readable file is the
    "at minimum" step). The `/workspace/.hf_token` (vLLM's HuggingFace token on `/engrfs`)
    `chmod 600` guidance is now documented in `start_vllm.sbatch` + `docs/deployment/engr/README.md`; the
    operator still has to apply it.

### Phase 2 — Instance modes: prod vs isolated test — ✅ cluster lane done

Give contributors a way to validate a change *while prod is live*, with a hard guarantee it
can't touch prod. A deployment now runs as one of two **instances**, selected by `deploy.sh`:

- ✅ **`deployment/deploy.sh` wrapper** — `deploy.sh` (prod) / `deploy.sh test`. Sets the Slurm
  `--job-name` (`agenticnetwork-prod` / `agenticnetwork-test`) and exports `DEPLOY_ENV` +
  `PORT_OFFSET` into `submit.sbatch`.
- ✅ **prod** — the canonical instance: `/engrfs` Postgres (real, credential-bearing state),
  base ports, committed `ads_daemon.yaml`.
- ✅ **test** — an isolated copy that may run **beside** prod: all ports offset by **+10000**
  (`submit.sbatch` instance-mode block), Postgres data dir on **node-local `/scratch/$USER`**
  (never `/engrfs`, so it *structurally cannot* reach prod's data), and an ADS config generated
  from `ads_daemon.yaml` with the offset server/routing/PG ports. Wired via `ADS_SERVER_ADDR` /
  `PGPORT` / `DATABASE_URL` exports that the entrypoints already honor (plus
  `REGISTRATION_DB_DSN` with the same value, for images built before the 2026-10-25 rename).
  Also fixed a latent bug: `entrypoint-postgres.sh` now starts the server on `PGPORT` (was always 5432).
- ✅ **Per-service image tags** — isolation also covers *which image* runs, so a dev can test a
  candidate build without clobbering the prod-facing `:latest`. Each service's tag resolves as
  `<SERVICE>_IMAGE_TAG` → `IMAGE_TAG` (instance-wide default; the future CI SHA tag) → the
  service default (`latest`, or `v4` for ads), via the `image_ref` helper in `submit.sbatch`.
  Workflow: `IMAGE_TAG=dev-alice bash deployment/build_and_push.sh planner` (the chat app's
  image, still named `planner`; publishes `…/agenticnetwork-planner:dev-alice`, leaving `:latest` alone), then launch the test instance
  with `PLANNER_IMAGE_TAG=dev-alice` — only the planner runs the candidate; every other service
  stays on `:latest`. `build_and_push.sh` honors `IMAGE_TAG` for the build side.

The cluster `test` instance is the single pre-ship validation path. (A laptop-local "Tier B"
tier was considered and **dropped** — not worth maintaining a second story.) (Update 2026-10-13:
a native laptop dev stack now exists — [`DEVSTACK.md`](DEVSTACK.md) — for code-level checks; the
cluster test instance remains the pre-ship path for deploy changes.)

**Reaching a test instance (no public URL).** The Apache + Shibboleth gateway
(`agenticweb.engr.wustl.edu`) proxies to exactly **one** upstream — the prod planner at
`<prod-node>:8090`, repointed by hand each prod deploy (`DOWNSTREAM #3`). It has no knowledge of
a test instance, so a test deploy **cannot** be reached at the public URL and **cannot** disturb
prod's front door — whichever node it lands on. Reach a test instance out-of-band by tunnelling
to its planner port (`18090`) through the login node, then browsing `http://localhost:18090`:
```
squeue -u $USER -n agenticnetwork-test -h -o '%N'                      # find <test-node>
ssh -J <wustlkey>@<login-host> <wustlkey>@<test-node> -L 18090:localhost:18090
```
(Two-hop — jump through the login node, SSH into the test node, forward its local `:18090`. Keep
the session open. Full step-by-step + `~/.ssh/config` shortcut in `docs/deployment/engr/README.md`.)
- Because that path bypasses Apache, there are **no Shibboleth identity headers**, so the planner
  would 401 every `/app`/`/messages` request. Therefore `submit.sbatch` sets
  **`CHAT_APP_ALLOW_ANON=1` for `DEPLOY_ENV=test` only** (prod always keeps the SSO gate;
  override with `CHAT_APP_ALLOW_ANON=0`; it also exports the old name, `PLANNER_ALLOW_ANON`,
  for older planner images). With that, the tunneled UI is usable as an anon/demo user.
- **Google-OAuth features don't complete in test:** `GOOGLE_REDIRECT_URI` points at the prod
  public URL, so "connect Google" bounces back to prod, not the test instance. Core chat + agents
  that don't need per-user Google creds work; the Google-connect flow doesn't.
- **Repoint footgun:** whoever updates Apache's upstream after a prod redeploy must target **prod
  specifically** (the `agenticnetwork-prod` job / port `8090`), not "whatever planner is up" —
  now that a second planner (test, `:18090`) can exist.

- ✅ **Custom-agent dynamic port pool** — `registration_service/port_pool.py` (and `fake_db.py`)
  read the range from `CUSTOM_AGENT_PORT_START`/`_END` (default 19200–19299); `submit.sbatch`
  offsets it for test (→ 29200–29299), so a test instance's spawned custom agents can't collide
  with prod's on a shared node. Test uses a separate Postgres, so the pool *counter* was already
  isolated — this closes the remaining collision on the actual bound host ports.

**Isolation notes:**
- ✅ **ADS gossipsub cross-peering — verified clean on ENGR.** Two ADS daemons on one node use
  different routing ports and *separate* Postgres `dir` DBs; the concern was whether gossipsub
  auto-peers them (e.g. via mDNS) and leaks routing between prod and test. Confirmed empirically:
  with prod + test both live, the test planner's `/agents` listed **only** test-port agents
  (`:19103`, `:29105–29108`) — no prod-port entries — so the directories do not cross-peer.
  (If this ever regresses, disable mDNS / set a distinct libp2p rendezvous for the test config.)

### Phase 3 — CI (GitHub Actions)

- ✅ **Interim deploy lock** — `deploy.sh` preflights `squeue` across *all* users **before
  calling `sbatch`** and refuses to submit if another job of the **same name** (i.e. same
  instance) is already running or queued. Prod and test differ by name, so they coexist; two
  prods (or two tests) do not — closing the shared-`/engrfs`-Postgres corruption path that
  per-user `--dependency=singleton` misses. Doing the check at submit time (not in the job body)
  gives an immediate refusal instead of a second job stuck `PENDING(Dependency)`. The in-job
  `squeue` preflight in `submit.sbatch` and the `--dependency=singleton` directive remain as
  backstops for anyone who bypasses `deploy.sh` and runs `sbatch` directly.
- ⏳ CI builds + pushes all images on merge to `main`, tagged by **commit SHA** (+ a moving
  `main` tag) → kills both the `:latest` clobbering and the "who has push creds" question.
- ⏳ Deploy becomes a **single serialized GitHub Actions job** using a concurrency group
  (`concurrency: { group: deploy, cancel-in-progress: false }`) — simultaneously *how* CI
  deploys and *the global lock*, so nobody deploys from a laptop. (The `squeue` preflight above
  remains the belt-and-suspenders guard even once CI exists.)

## Decisions so far

- ✅ **Test-lane namespace token** — per **`$USER`**.
- ✅ **Test Postgres** — cluster node-local-scratch (`/scratch/$USER`), physically separate from
  prod by construction. A shared always-on test host and a laptop-local tier were both rejected.
- ✅ **CI platform** — **GitHub Actions**.
- ✅ **Deploy trigger + lock** — a concurrency-grouped GitHub Actions deploy job (same
  mechanism serves as both the deploy path and the global lock); `squeue` preflight check as
  the pre-CI interim.

## Still open

1. ⏳ **Registry** — which org registry (IT conversation in progress). Everything else in
   Phase 0 can be staged behind an `IMAGE_REGISTRY` var regardless.
2. **GitHub Actions runner reach** — do the runners need to reach the registry only, or also
   trigger the cluster deploy (self-hosted runner on-cluster vs. an SSH/`sbatch` step from a
   hosted runner)? Resolve when Phase 3 starts.

---

## Future work — de-personalize the GitHub write identity (GitHub App)

> **Status: planned, not started (2026-07-16).** The DockerHub side is already group-owned
> (`di2washu`, Phase 0). The remaining personal coupling is the **GitHub PAT** the registration
> service uses to commit build contexts to `agent-directory` — see the "GitHub write identity"
> row in the coupling inventory above. This section captures the agreed plan so we can pick it up
> later; no code has changed.

**The three identity surfaces, and what each fix reaches:**

| Surface | Where it lives | Fixed by |
|---|---|---|
| Commit author on `registered_agents/…` | `supervisor.py` `_github_commit_files` (no `author`/`committer` → inherits the token owner) | replacing the token |
| Triggering **actor** of the build workflow | bound to whoever's token pushed (follows the commit token) | replacing the token |
| DockerHub push account | `agent-directory` workflow secrets (`DOCKERHUB_USERNAME/TOKEN`) | ✅ already `di2washu` (shared) |

The first two collapse into one decision: **replace the personal PAT with a GitHub App.** A
lightweight "keep the PAT, just set an explicit bot `author`/`committer`" was considered and
rejected — it relabels the commit but leaves the Actions **actor** as the person, so it does not
fully decouple. An App does: commits *and* the run actor both become `agenticnetwork-bot[bot]`
natively (the Git Data API attributes commits made with an installation token to the app bot, so
no `author` override is needed).

**Chosen approach — GitHub App (`agenticnetwork-bot`):**

*Setup (org owner, one-time — cannot be done from the repo):*
- Create a GitHub App on the `washu-dev` org: webhook **off**, single permission **Contents:
  Read and write** (Metadata: Read is auto), installable "only on this account."
- Note the **App ID**, generate a **private key** (`.pem`), and **install** the App scoped to
  **`agent-directory` only** (note the Installation ID, or let the code auto-discover it via
  `GET /repos/{repo}/installation`).

*Deployment wiring (secrets travel as env vars — `submit.sbatch` sources the secrets file):*
- Add to the prod secrets file: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_B64`
  (`base64 -i key.pem | tr -d '\n'` — base64 avoids multiline/quoting pain across
  `source` → `srun`), optional `GITHUB_APP_INSTALLATION_ID`. `GITHUB_REPO` stays in `.env`.
- After verification, remove the personal `GITHUB_TOKEN`. (`GITHUB_ISSUES_TOKEN`, used for the
  bug-report → Issues flow, is a **separate** token — migrating it to the App is a later follow-up.)

*Code changes (registration_service):*
- `requirements.txt`: add `PyJWT>=2.8.0` + `cryptography>=42.0` (RS256).
- `supervisor.py`: add a cached installation-token minter (App JWT `iss=APP_ID`, `iat-60`,
  `exp+540`; `POST /app/installations/{id}/access_tokens`; cache until ~60s before the ~1h
  expiry). `_github_commit_files` and `_delete_from_github` fetch the token from it instead of
  `os.environ["GITHUB_TOKEN"]`.
- `_oob_build_enabled()`: accept **App creds OR `GITHUB_TOKEN`** so the change can land inert
  (PAT keeps working until the App secrets are populated — a clean, reversible cutover). While
  the gate is false, an approval on a build backend (cluster, Cloud Run) is refused with
  `LaunchRefused` (503).
- Update `deployment/secrets.env.example` + this doc.

*Verify:* register a test agent → the `registered_agents/…` commit and the "Build custom agent
image" run both attribute to `agenticnetwork-bot[bot]`, nothing reads a personal handle.

**Gotchas noted for later:**
- The external push still triggers the workflow — the "`GITHUB_TOKEN` pushes don't cascade" rule
  only applies to the *in-Actions* ephemeral token, not an App installation token used by the
  (external) registration service.
- Installation tokens expire ~1h → the minter must cache-with-refresh so a long-lived
  registration process never sends a stale token.
- **Durable end state:** post-GCP-migration, DockerHub → Artifact Registry with GitHub OIDC /
  Workload Identity Federation removes the last long-lived push secret entirely (see
  [`docs/deployment/gcp/MIGRATION.md`](deployment/gcp/MIGRATION.md), "Images" decision row).
  App (GitHub) + OIDC (registry) = zero personal credentials in the path.

---

## Pre-merge validation

**Validated on ENGR ✅** — the gates below were exercised on the cluster and passed: group
registry (`di2washu`) pulls, **prod baseline** healthy, **deploy lock** refuses a duplicate at
submit time, **test-beside-prod** isolation (offset ports + node-local Postgres), and **gossipsub
isolation** (test `/agents` shows only test-port agents). The checklist is retained as the
repeatable runbook for future changes. Optional spot-checks not exercised are noted below.

### Setup (one-time)
- [ ] **Registry.** Once IT confirms the group registry, set `IMAGE_REGISTRY` in
      `deployment/.env` and build + push every image: `bash deployment/build_and_push.sh` (infra),
      plus the specialist agents from the `agent-directory` repo. Note the ADS image is still
      pinned `:v4` in `submit.sbatch` — push that tag or update it.
- [ ] **Secrets.** Create the prod secrets file `/engrfs/project/di2/AgenticNetwork/deployment/secrets.env`
      (`chmod 0640`, group-owned) with the real `CREDENTIALS_ENCRYPTION_KEY` (**the existing prod
      key** — a new one makes stored creds undecryptable), `GITHUB_TOKEN`, `GITHUB_ISSUES_TOKEN`, `GOOGLE_CLIENT_SECRET`.
      Confirm they are **removed** from `deployment/.env`. `chmod 0600 /engrfs/project/di2/AgenticNetwork/.hf_token`.
- [ ] **Test scratch.** Confirm `/scratch/$USER` exists and is writable on the target node.

### Prod baseline (prod behavior must be unchanged)
- [ ] `deployment/deploy.sh` → tail the log. Confirm: `Instance : prod (port offset 0)`,
      `Loaded secrets from …`, vLLM auto-discovered (`vLLM discovered: http://…`), Postgres on
      `:5432` mounting `/engrfs/project/di2/postgres_data`, ADS on `:8888`, all agents up.
- [ ] Planner reachable at `:8090`; log in via SSO; **confirm an existing user's stored Google
      connection still works** (proves `CREDENTIALS_ENCRYPTION_KEY` is the right one).

### Deploy lock
- [ ] With prod live, run `deployment/deploy.sh` again → it must **refuse immediately at submit
      time** ("a 'agenticnetwork-prod' deployment is already live or queued") and **not submit a
      job** (no leftover `PENDING` job to clean up). Test from a *second* user too, to confirm
      it's cross-user, not just the per-user `--dependency=singleton` backstop.

### Test instance beside live prod
- [ ] With prod still live, `deployment/deploy.sh test`. Confirm: `Instance : test (port offset
      10000)`, ports offset (planner `:18090`, ADS `:18888`, Postgres `:15432`), Postgres data on
      `/scratch/$USER/agn-test/...` (**not** `/engrfs`), a generated `ads_daemon.test.yaml`.
- [ ] Planner reachable at `:18090`; **prod at `:8090` unaffected** throughout.
- [ ] **Isolation proof:** the test `registration` DB is empty/fresh; prod's data is intact and
      untouched. A second test deploy is refused ("agenticnetwork-test already live").
- [ ] **Candidate image:** `IMAGE_TAG=dev-$USER bash deployment/build_and_push.sh planner`, then
      `PLANNER_IMAGE_TAG=dev-$USER deployment/deploy.sh test` (deploy.sh propagates it via
      `--export=ALL`). Confirm the banner's `Img tags:` line shows the override, the test planner
      runs the candidate, and prod's `:latest` planner is untouched.

### Known-gap checks (may become follow-up tickets, not necessarily blockers)
- [ ] **Gossipsub cross-peering:** with prod + test ADS both up, confirm prod's directory does
      **not** list test agents and vice versa (`dirctl` routing list against each). If it leaks,
      disable mDNS / set a distinct rendezvous for test before relying on concurrent test.
- [ ] **Custom-agent ports:** spawn a custom agent in the test instance and confirm it lands in
      `29200–29299` (offset), not prod's `19200–19299`.
- [ ] **vLLM fallback:** confirm `VLLM_AUTODISCOVER=0` uses the `.env` value verbatim.

### Teardown & merge
- [ ] `scancel` the test job; confirm prod stays healthy; confirm `/scratch/$USER/agn-test` is
      cleaned up.
- [ ] Update this doc's ⏳/✅ statuses with what actually held, then merge to `main`.
