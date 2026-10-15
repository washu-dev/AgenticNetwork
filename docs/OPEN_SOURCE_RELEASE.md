# Open-source release plan

Status as of **2026-10-15**. This is a working document for the maintainers and the
agents helping them. Update it as decisions land, and rewrite or remove it before the
repository goes public.

## Where things stand

- **Goal:** release AgenticNetwork as open source.
- **Approval:** the PIs and DI2 are waiting on the engineering clean-up before the
  release goes ahead. No license is attached yet. The approved license, the copyright
  line and any WashU sign-off still have to be recorded before visibility changes.
- **This week (from 2026-10-12):** a fix-up sprint. It works through the goals already
  set in the maintainers' release checklist, with the **full-pipeline test suite**
  (below) as the main deliverable to ship with the release.
- **Code state:** branch `release-prep/full-chain`, on top of `main` = `ebf1b55`. Save point
  `e7d426a` (2026-10-13): release hygiene (`.gitignore`, `.dockerignore`), planner identity
  and `/ready`, native agent spawning and `/ready` in registration, the dev stack
  (`scripts/stack`), the full-chain test (`tests/pipeline/`) and CI. The 2026-10-13
  safe-cuts pass on top of it removes dead code, stale docs and unused dependencies, and
  lands the security fixes listed below.
- **Tests:** the component suites, `devstack/tests` and the full-chain test pass offline.
  CI (`.github/workflows/tests.yml`) runs them, plus a repo-hygiene job, on every PR and
  push to `main`. Commands: `CLAUDE.md` §10.
- **Goals 1–3** (below): tier 1 of Goal 1 is done; the full-chain test passed 20 runs in a
  row locally on 2026-10-13. `scripts/stack` covers Goals 2 and 3 for the native tier
  ([`DEVSTACK.md`](DEVSTACK.md)).
- **2026-10-15 pass (on `release-prep/full-chain`, not yet pushed):** the last open release blocker is fixed
  (credentials no longer reach skeleton agents' LLM prompts); `down`/`reset` in the dev stack
  never stop another stack and keep recovery state when a stop fails; three black-box scenarios
  (credential scoping, unreachable agent, restart) in `tests/pipeline/test_scenarios.py`;
  coverage summaries in CI; tier 2 runs as a Docker Compose stack
  ([`deployment/COMPOSE.md`](deployment/COMPOSE.md), `deployment/compose/smoke.sh`, verified
  locally on arm64, CI workflow `compose.yml` not yet run on GitHub); a generic Cloud Run guide
  ([`deployment/gcp/DEPLOY_YOUR_OWN.md`](deployment/gcp/DEPLOY_YOUR_OWN.md), not re-run live);
  public-facing `README.md`, `CONTRIBUTING.md`, [`TESTING.md`](TESTING.md), docs index and
  `SECURITY.md` (reporting policy first).

### Decided 2026-10-13 (Ronak)

- At the skeleton merge, drop `generator/` from the merged skeleton.
- The GCP Cloud Run and ENGR cluster code paths stay. Cloud is a critical leg of the
  project going forward; the ENGR cluster is the live deployment.
- This pass makes safe cuts only: production behaviour on ENGR and Cloud Run is unchanged
  unless noted below, and new settings default to today's behaviour. The bigger, breaking
  simplifications wait in "Deferred simplifications".

### Checks before the next ENGR or Cloud Run image push

- **Registration image:** it no longer installs `postgresql-15` (Postgres runs in its own
  container). Rebuild with a personal `IMAGE_TAG`, then check `GET /health` on an ENGR test
  instance (`deployment/deploy.sh test`) and on a non-traffic Cloud Run revision before
  pushing `:latest`.
- **Planner image:** six unused pins left `requirements.txt`; the first clean install
  should pass `pip check`.
- **Approve → commit:** run one live approval on a test instance. `build_manifest` changed,
  and the unit tests do not exercise the live GitHub commit.
- **Tool-loop agents** (`llm` + uploaded tools) now advertise streaming, but only in images
  rebuilt with this skeleton. Of the vendored copies in `agent-directory`, only
  `registered_agents/echo-test` has the tool-loop path, and it still says `streaming=False`.
- **dirctl timeouts:** every planner `dirctl` call is now killed after
  `DIRCTL_TIMEOUT_SECONDS` (default 60 s; before, no limit), and `/ready` kills its probe
  after 4 s. Raise the setting if a slow directory needs longer.
- **Stored agents:** `entry_file` must now be a bare `*.py` name, and stored configs are
  validated when read. All 17 registered agents use `handler.py`; before deploying, check
  that no production row has a nested `entry_file`.
- **Registration UI assets:** the unused `/static` mount is gone (the UI is the single
  page served at `/`).
- **Credential scoping:** agents that declare no credentials now get none (before, the
  whole bundle). The email agent reads `google_oauth` without declaring it, so ENGR's real
  `deployment/.env` must add `PLANNER_UNSCOPED_CREDENTIAL_AGENTS=email-agent,geospatial-agent`
  (as in `.env.example`; ids as listed by `GET /agents`), or the email agent stops getting
  each user's Google token and falls back to its on-disk token file, if any. Check the
  planner log for `credentials_unscoped_legacy_agent`. The geo agent is listed only to keep
  today's behaviour: it reads the pre-catalog names `google_api`/`google_maps`, which the
  planner's DB migration renamed. Cloud Run runs no email or geo agent, so it needs no list.
- **Bug-report repo:** `GITHUB_ISSUES_REPO` no longer defaults to `washu-dev/AgenticNetwork`.
  If ENGR's real `.env` still has the old example's value, change it to a private repo (or
  blank it) before bug reporting is turned on, and before visibility changes. On Cloud Run,
  set it wherever `GITHUB_ISSUES_TOKEN` is set.

## What ships

- **This repo, with `agent-skeleton` nested inside it at `agent_skeleton/` via
  `git subtree`** (decided 2026-10-12). The standalone
  [`washu-dev/agent-skeleton`](https://github.com/washu-dev/agent-skeleton) history is
  merged into `agent_skeleton/`. One plain clone gets everything, and changes can still
  be split back out with `git subtree push`. Only this repo needs to go public; the
  standalone repo can stay internal and its open hackathon PRs and issues never become
  public.
- **Proposed earlier, still to confirm:** `agent-directory` (the specialist agents and
  the hackathon-registered agents) stays out, as do model weights, cluster logs and
  WashU-specific operations details. Open: whether the ops docs (`docs/deployment/engr`,
  `gcp`, `DOWNSTREAM_CONSIDERATIONS`, `MULTI_CONTRIBUTOR`, `TESTING_CHANGES`) ship publicly
  or move to a private ops repo. The code paths stay either way.
- **Scrub before visibility changes:** the email agent card's send example held two
  real-looking personal email addresses. The planner fixture now uses `example.com`
  addresses (2026-10-13), but the originals remain in this repo's history (from `a7786fb`
  on) and in `agent-directory/email_agent/email_agent.card.json`. Filter them out of the
  release history and fix that card.

### Before the subtree merge

The two copies have diverged, so they must be reconciled first.

| Only in this repo's `agent_skeleton/` | Only in the standalone `agent-skeleton` |
|---|---|
| `container_runner.py` (the ENTRYPOINT of every built custom agent), `generator/`, the tool-loop `spec.py`, `system_tools/call_endpoint.py`, flat module layout, `test_generator` / `test_manager` / `test_operations` | `core/ custom/ tool_loop/ endpoint_wrapper/` layout, the `@tool` decorator, docs (`REGISTERING.md`, `KNOWN_ISSUES.md`), the 2026-07-27 hackathon fixes |

1. Port the repo-only pieces into the standalone layout, including the 2026-10-13
   `container_runner._tool_loop_card` fix (streaming). Keep the import path
   `agent_skeleton.container_runner`, because built images and
   `registration_service/archive.py` (the impersonation guard) depend on it.
2. Make both test suites pass in the standalone repo. Living at `agent_skeleton/`, it
   also avoids KNOWN_ISSUES #2 (its tests pass only from a folder of that name).
3. Replace the in-repo directory with a `git subtree add --prefix=agent_skeleton`.
   Without `--squash`, the standalone history (including the slide deck removed in
   `af3f25e`) becomes part of this repo's history. So either squash, or filter that
   history first. The release history is filtered anyway before anything goes public.
4. Update everything that reads the folder: `registration_service/builder.py` copies
   `agent_skeleton/` into each custom agent's build context, `backend.py` resolves it
   from the repo root, and the Dockerfiles install it.

## Sprint priorities

1. **The full-chain test, the one-command start and the laptop workflow** (Goals 1–3).
   Tier 1, the native stack and tier 2 (Docker Compose, real Postgres and `dirctl`, verified
   locally 2026-10-15) are done. Still to do: a tier-3 real-model run, `add-agent` for an
   agent folder, and the scenarios not yet covered (below).
2. **Security fixes that must land before release**, each with a regression test:
   - Fixed: a request-body `user_id` no longer overrides the logged-in identity (planner
     tests and the full-chain test).
   - Fixed: `POST /confirmations` returns 403 to anyone but the conversation owner, for
     confirm and cancel (`test_confirmations_forbid_non_owner`). Small gap: a cancel on
     another process after a failed best-effort history save is not owner-checked.
   - Fixed: an upload's `entry_file` must be a bare `*.py` name; `../x.py`, absolute paths
     and newlines get 422 (`test_custom.py::test_entry_file_must_be_bare_name`).
   - Fixed: the planner's `/register/{path}` proxy no longer forwards registration's
     `/internal/*` routes, through which any signed-in user could mark an agent live and
     published without approval (`test_register_proxy_never_forwards_internal_routes`).
     Still open: `/internal/*` itself has no auth and relies on network isolation. A
     loopback check in registration would not help ([`SECURITY.md`](../SECURITY.md) §2); a
     shared secret needs `deployment/spawn_agent_loop.py`, `submit.sbatch` and registration
     changed together.
   - Fixed: `.dockerignore` keeps `*.local.env`, keys, credential/token JSON and local
     databases out of image build contexts.
   - Fixed: an agent gets only the credential types it declared and the user granted; one
     that declares none gets none, and without a gate or login identity only the declared
     types pass. Legacy agents named in `PLANNER_UNSCOPED_CREDENTIAL_AGENTS` still get the
     whole bundle, with a log line (`test_credential_enforcement.py`:
     `test_undeclared_agent_gets_no_credentials`, `test_no_gate_passes_only_declared_types`,
     `test_no_identity_passes_only_declared_types`,
     `test_allowlisted_legacy_agent_gets_full_bundle`).
   - Fixed (2026-10-15, from `washu-dev/agent-skeleton` PR #29 by raneemousa3): skeleton-engine
     agents strip `credentials`, `authorization`, `secret(s)` and `token` keys from request,
     message and part metadata and DataPart data before the LLM prompt
     (`agent_skeleton/executor.py::_model_visible_metadata`; `agent_skeleton/tests/test_executor_metadata.py`).
     Registration's runner no longer logs a bad config's values (`registration_service/tests/test_prompt_secrets.py`).
     Still open: the other metadata keys, including `user_id` (the user's email), reach the
     model provider; an allowlist of model-visible keys would close that.
   - Fixed: user bug reports no longer default to this repo. `GITHUB_ISSUES_REPO` has no
     default; unset, `POST /issues` returns 502 without calling GitHub. Issue bodies still
     include the reporter's email, prompts and trace, so the repo must be private
     (`test_issues_endpoint.py`: `test_create_issue_requires_repo`,
     `test_issues_without_repo_502_and_no_github_call`).
3. **CI:** done. `.github/workflows/tests.yml` runs the unit suites (planner, registration,
   skeleton, ADS, devstack), repo hygiene and the native full-chain test.
4. **Hygiene:** done. The tracked `.gitignore` covers `build/`, `dist/`, `Local/`, `.env*`
   (except examples), keys, model weights and the dev-stack state dirs; CI's hygiene job
   checks it.
5. **Merge the agent_skeleton copies** and bring the standalone history in with
   `git subtree` (see "Before the subtree merge").

### Carried over from retired plan docs (2026-10-13)

The wrapper-presets and bug-reporting plans were deleted; their decisions are D12 and D13
in [`DECISIONS.md`](DECISIONS.md). Still open from them:

- Preset tools: decide the security posture before adding any network- or
  credential-touching tool.
- The multi-endpoint manager (`spec.multi_endpoint_wrapper_spec`) is engine-only and
  unit-tested, with no registration or UI path (`tools.source = "http"` covers one API's
  operations). Wire it or drop it.
- The generator lacks `preset_tools`, `endpoints[]` and catalog vendoring (moot if
  `generator/` is dropped at the skeleton merge).
- One real-model run of a preset-tool `llm` agent (tier 3).
- Document presets and the manager in the skeleton docs at the subtree merge.
- Bug reporting: Stages B and C (a cluster test instance, then prod) are not recorded as
  done; the procedure is in `docs/deployment/engr/README.md` "Verifying bug reporting".
  Later, move `GITHUB_ISSUES_TOKEN` to the GitHub App ([`MULTI_CONTRIBUTOR.md`](MULTI_CONTRIBUTOR.md)).
- GCP Step 10 (front door): the repo holds only an outline
  (`docs/deployment/gcp/IMPLEMENTATION_CLOUDRUN_BACKEND.md`); the full runbook was kept
  outside the repo. Recreate it before resuming cloud work.

## Full-pipeline test suite

### The three testing goals (2026-10-12)

**Goal 1: a full-chain test.** One test does everything a real first-time user's
session involves, in order, and checks each step:

1. **Start the system from nothing:** fresh database, empty directory, no services
   running.
2. **Create a user:** the user's first authenticated request creates their account.
3. **Send a whole prompt:** the user sends a complete request through the chat API,
   the same way the browser does.
4. **Get a response:** the planner discovers a specialist agent, delegates to it over
   A2A, and returns its answer to the user. The answer is also saved in that user's
   conversation history.
5. **Shut down cleanly:** nothing is left running and no state leaks into the next run.

By default it runs offline with a deterministic mock LLM, so it can run on every PR.
The same test can run against a real model when one is configured.

**Goal 2: start the system on command.** "Start the system" becomes one command that
is reliable enough for an agent, a script or CI to run whenever asked, and trust:

- It returns only when every service is healthy (exit code 0). Otherwise it fails
  quickly, with a message that names the broken service and points to its logs.
- It is idempotent: running it while the system is up changes nothing, and `down`
  then `up` works. After a restart, registered agents are discoverable again.
- It reports status in a machine-readable form (JSON), so an agent can check what is
  running without parsing logs.
- It has no hidden dependencies: no WashU cluster, SSO, personal files or keys. It
  defaults to the mock LLM.
- Reliability is measured, not assumed: it counts as done when the full-chain test
  passes many runs in a row on a clean machine.

**Goal 3: easy hands-on testing on a laptop.** A maintainer can try the whole system
by hand on a Mac in a few minutes. This replaces the untracked laptop prototyping stack
from summer 2026 with a tracked, documented one.

- **One command** brings up everything, and the chat UI opens at `localhost`, already
  signed in as a local dev user. There is no SSO and no cluster.
- **The LLM is switchable** with one setting: the mock LLM (offline, free, scripted
  answers), a hosted OpenAI-compatible API key, a local model (for example Ollama), or
  a tunnel to the cluster's vLLM.
- **Seed agents** are registered automatically, so there is something to talk to right
  away.
- **Trying an agent is one command:** run a folder from the `agent-directory` repo, or
  any local agent folder, through the real registration path, minus the GitHub
  commit. It then appears in the UI. (Today `add-agent` takes a JSON registration body.)
- **Everyday controls:** `status`, `logs <service>`, `reset` (wipe to a clean state)
  and `down`. Code changes show up without rebuilding anything: the dev stack runs
  natively from the checkout.

The three goals share one start command. The full-chain test, an agent asked to
"start the system", and a person trying it by hand all use the same path, so the test
also checks the command people actually use.

### Scope of the suite

- **Request path:** user message → planner → directory search → Agent Card → credential
  gate → A2A call → specialist agent → final answer.
- **Registration path:** register → (build) → spawn → publish → routed → discoverable
  by the planner.

**Principles:** deterministic and offline by default (mock LLM, in-memory DB, the fake
directory); no real keys; the same scenarios rerun at higher tiers.

| Tier | Runs | What is real |
|---|---|---|
| 1. Native (done) | Every PR | Planner, registration service and skeleton agents as local processes (`scripts/stack`); mock LLM; in-memory DB; fake `dirctl` |
| 2. Containers (done locally) | PRs touching services or deployment, weekly (`compose.yml`) | The service images, real Postgres and the real `dirctl` daemon via Docker Compose (`deployment/compose/smoke.sh`); mock LLM |
| 3. Real LLM | Manual or nightly, opt-in | One round trip with a hosted or local model |

**Covered by tier 1** (`tests/pipeline/test_full_chain.py`, driving `scripts/stack`): start
from nothing; the first signed-in request creates the user; a prompt reaches the seed
agent and the answer is saved for that user only; streaming; a newly registered `llm`
agent receives the prompt that matches it; a body `user_id` is ignored when a login
identity exists; `down` leaves nothing running. `tests/pipeline/test_scenarios.py` adds: a
declaring agent receives only its scoped credentials and an undeclared one none; an
unreachable agent (refused or silent) yields a clear error within the A2A timeout; after
`down` and `up`, a registered agent is listed once and answers. Unit tests cover the
owner-only confirmation and the refused `entry_file` and impersonating uploads.

**Still to add:**

- Each handler type: `llm` (presets, HTTP tools, uploaded tools), `proxy`, `custom` upload.
  Legacy type names normalize on read.
- An agent built from the skeleton template, through each path (`endpoint_wrapper`,
  `tool_loop`, `custom`), works end to end.
- `ask_user` and confirmation flows across processes.
- Failures: the step limit holds.

Security scenarios that fail today are added as strict expected failures and flipped
when their fix lands.

## Deferred simplifications

Bigger or breaking cuts found in the 2026-10-13 review, kept for later. Sizes are rough
lines removed or rewritten.

| Simplification | ~Lines | Waits on |
|---|---|---|
| **Protocol and directory** | | |
| One shared directory/A2A package (the planner fork is the superset): delete `agent_directory_service/services/`, repoint its scripts and tests, fold in `ads_utils` behind a shim, dedupe the admin check and credential catalog | 3,900 | one `a2a-sdk` pin; a chosen package and Docker build context |
| The planner's `ADS_BACKEND=sdk` path and the ADS `agntcy-dir`/buf.build deps (unused today) | 100 | how the GCP directory client gets built |
| Collapse the ADS address aliases into `ADS_SERVER_ADDR` | 20 | a coordinated `agent-directory` change and cluster `.env` update |
| **Skeleton subtree and runner merge** | | |
| The `git subtree` merge | 1,500 | "Before the subtree merge" above |
| Merge registration's `runner.py` + `handlers/` with `container_runner.py`; one A2A app factory; isolate the ENGR-only code | 580 | the subtree merge; a stable full-chain test |
| Drop the generator (with `repo_emitter`'s unused `import os`); make `AgentSpec` mandatory and drop the demo lane | 1,030 | the generator and template decisions at the merge |
| The multi-endpoint manager | 260 | wire it or drop it (above) |
| Refresh the 17 vendored skeleton copies in `agent-directory/registered_agents/` | 2,200 | a re-vendor pass, or the subtree merge |
| **Planner structure** | | |
| Split `create_app` (nested routes) into routers with explicit app state | 600 | an agreed layout |
| Move the email-specific heuristics out of the generic loop | 150 | cards declaring input fields and a write tag |
| Run `dirctl` off the event loop (calls are now time-bounded) | 40 | a cache TTL and concurrency tests |
| Drop the `channel` field everywhere | 40 | a written A2A metadata contract |
| Retire `planner_agent/scripts/doctor.sh` | 150 | `scripts/stack doctor` covering real ADS and LLM reachability |
| **Registration internals** | | |
| Retire the legacy handler-type names and config shapes, with `container_runner`'s legacy envelope | 230 | a one-time DB migration on ENGR and GCP |
| Put all SQL behind `db.py` helpers, then replace `fake_db.py`'s SQL matching | 260 | one typed pool-exhausted error; a tier-2 Postgres test |
| Drop `build_manifest`'s dict-config support | 25 | typed test fixtures |
| **Dependency and version upgrades** | | |
| `dir`/`dirctl` v1.2 → v1.7, then delete the stored-but-not-routed workarounds (planner backend, ADS repair/watch tooling) | 1,550 | re-validating publish and discovery on ENGR and GCP |
| One `a2a-sdk` line (0.3.26 everywhere, then 1.x), replacing the deprecated `A2AClient` and the skeleton's import guard | 270 | the full-chain test on the new pins; rebuilt agent images |
| Test-only deps into `requirements-dev.txt`; the planner's pip freeze becomes direct deps plus a lock file | 50 | a locking tool, with CI and docs updated together |
| **Images, deploy config and hygiene** | | |
| Planner image on `python:3.x-slim` instead of miniconda; one Python version across images | 45 | an ENGR test deploy and a Cloud Run staging deploy |
| One ADS image tag (`submit.sbatch` pulls `:v4`, `build_and_push.sh` pushes `:latest`) | 2 | confirming they match |
| Generate `ads_daemon.gcp.yaml` from `ads_daemon.yaml` | 70 | the GCP runbook and VM startup |
| Prune cluster- and person-specific `.gitignore` rules; dedupe the secrets rules | 16 | checking the ENGR checkout |
| Delete the untracked `Local/` laptop stack | — | Ronak (it holds local secrets) |
| **Docs restructure** | | |
| Fold `CLAUDE.md` into `CONTRIBUTING.md`, `ARCHITECTURE.md` and the deployment docs; keep a short agent entry file | 450 | an agreed outline; a `§`-reference checker |
| Move `agent-directory`-specific content (specialist envs, LLM matrix, caveats, hand-built cluster path) to that repo | 540 | a docs home there |
| Replace `agent_skeleton/README.md` and `CLAUDE.md` with the standalone repo's docs | 365 | the subtree merge |
| Rewrite the protocol-duplication docs | 80 | the protocol consolidation |
| `SECURITY.md` becomes a public policy; this file becomes a roadmap/changelog or goes | 260 | the security fixes; release sign-off |
| A docs link and `§`-reference check in CI | 60 | the doc fixes landing |
| Stale docs in the sibling repos: `agent-directory/ads_utils/README.md`; `agent-skeleton/CARD_AUTHORING_PLAN.md` (converter cleanup; a card path now under `planner_agent/tests/fixtures/agent_cards/`); `KNOWN_ISSUES.md` (a lab IP) | 6 | a session in those repos |
| **Ops docs scope** | | |
| Ship or move the WashU ops docs (`docs/deployment/engr`, `gcp/*`, `DOWNSTREAM`, `MULTI_CONTRIBUTOR`, `TESTING_CHANGES`) | 3,300 | Ronak and the PIs/DI2; then scrub or move, and filter history |
| Merge the three ENGR test-instance docs; consolidate the GCP design and progress logs | 850 | the decision above; resuming GCP work |
