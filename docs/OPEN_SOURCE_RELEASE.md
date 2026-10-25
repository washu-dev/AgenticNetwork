# Open-source release plan

Status as of **2026-10-16**. This is a working document for the maintainers and the
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
- **Code state:** everything below is on `main`. It holds the 2026-10-13 save point
  `e7d426a` (release hygiene in `.gitignore` and `.dockerignore`, planner identity and
  `/ready`, native agent spawning and `/ready` in registration, the dev stack
  `scripts/stack`, the full-chain test `tests/pipeline/` and CI), the 2026-10-13 safe-cuts
  pass (dead code, stale docs and unused dependencies removed; the security fixes listed
  below), the two 2026-10-15 passes and the 2026-10-16 confirmation follow-ups.
- **Tests:** the component suites, `devstack/tests` and the pipeline suite (full chain and
  scenarios) pass offline. CI (`.github/workflows/tests.yml`) runs them, plus a repo-hygiene
  job, on every PR and push to `main`; it first passed on GitHub for `d069aaf` (2026-10-15).
  The tier-2 workflow (`compose.yml`) now also runs on pushes to `main` that touch service or
  deployment code; its first GitHub run passed on `28ab3ce` (2026-10-15, amd64, about 3 min). Commands: `CLAUDE.md` §10.
- **Goals 1–3** (below): tier 1 of Goal 1 is done; the full-chain test passed 20 runs in a
  row locally on 2026-10-13. `scripts/stack` covers Goals 2 and 3 for the native tier
  ([`DEVSTACK.md`](DEVSTACK.md)).
- **2026-10-15 pass (on `main` since 2026-10-15):** the last open release blocker is fixed
  (credentials no longer reach skeleton agents' LLM prompts); `down`/`reset` in the dev stack
  never stop another stack and keep recovery state when a stop fails; three black-box scenarios
  (credential scoping, unreachable agent, restart) in `tests/pipeline/test_scenarios.py`;
  coverage summaries in CI; tier 2 runs as a Docker Compose stack
  ([`deployment/COMPOSE.md`](deployment/COMPOSE.md), `deployment/compose/smoke.sh`, verified
  locally on arm64, and on emulated amd64 with a conda-free planner image, because the shipped
  planner Dockerfile hangs at `mamba create` under Rosetta); a generic Cloud Run guide
  ([`deployment/gcp/DEPLOY_YOUR_OWN.md`](deployment/gcp/DEPLOY_YOUR_OWN.md), not re-run live);
  public-facing `README.md`, `CONTRIBUTING.md`, [`TESTING.md`](TESTING.md), docs index and
  `SECURITY.md` (reporting policy first).
- **Engineering finalize pass (2026-10-15, evening):** three more security fixes (another user's
  chat "yes" or "no" could answer someone's pending action with the replier's credentials; two
  answers to one pending action arriving together both ran it; the user's email reached
  skeleton agents' model provider); the planner reuses one LLM client per
  endpoint and closes it on shutdown (per-call clients could hang an A2A call); cluster startup
  recovery keeps `llm` agents with uploaded tools; seven more pipeline scenarios (proxy agent,
  preset tool, step limit, `ask_user`, owner-only confirmation by button and by chat, two
  confirmations at once); release
  hygiene (no real addresses in code, tests or templates; no retired-plan references; neutral
  credits; per-project Compose images); a fresh-clone run of the quickstart, dev stack and
  Compose docs; and a history and hygiene audit (below). With this pass the engineering side is
  finished for the release candidate. What remains is decisions and access: the license and
  sign-off, the ops docs, the history approach, and the live checks listed below.
- **Confirmation follow-ups (2026-10-16):** the two small gaps the finalize pass left in
  pending confirmations are closed: a late "yes" on another planner process is told the
  first answer is still running, and the confirm button names its prompt, so a click on an
  old prompt runs nothing (security list below).

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
- **Chat app image** (`Dockerfile.chat-app`, image `agenticnetwork-chat-app`): six unused pins
  left `requirements.txt`; the first clean install should pass `pip check`.
- **Chat app start command:** images built after the `planner_agent/` → `chat_app/` rename
  hold `chat_app/` and `agentnet/` and start with `python -m chat_app` (`entrypoint-chat-app.sh`
  does this on ENGR and Compose). The old Cloud Run `planner` service, started with
  `--args=-m,planner_agent.scripts.run_server`, is replaced by a `chat-app` service started with
  `--args=-m,chat_app` ([gcp/README.md](deployment/gcp/README.md#dev-workflow), "Old `planner`
  service"; [DEPLOY_YOUR_OWN.md](deployment/gcp/DEPLOY_YOUR_OWN.md) step 10). Since 2026-10-25 the
  image is `agenticnetwork-chat-app`, so nothing pushes a newer `agenticnetwork-planner:latest`
  for the old service to pull. The old `MAIN_AGENT_*` setting names still work.
- **Approve → commit:** run one live approval on a test instance. `build_manifest` changed,
  and the unit tests do not exercise the live GitHub commit. Each approval's commit now replaces
  the whole `registered_agents/<slug>/` directory (files the new upload no longer has are
  deleted) and adds `registered_agents/<slug>/.approval`, so every approval rebuilds the image.
  Check that the test approval's commit shows `.approval` and that CI rebuilt the image.
- **Per-commit image tags (2026-10-22):** the registration service now waits for, and runs,
  the custom-agent image tagged with its approval's `agent-directory` commit SHA, not `:latest`.
  Push `agent-directory`'s `build-custom-agent.yml` change (it tags `:<commit SHA>` as well as
  `:latest`) before deploying this service; otherwise new approvals stay `building`, and an
  agent approved meanwhile needs a restart. On a test instance, check that an approval's image
  appears on Docker Hub as `:<commit SHA>` and that the agent runs it. Before running an older
  version against the same database (a rollback), clear the column with
  `UPDATE agents SET build_commit = NULL`: older versions neither record nor clear it, so a row
  they approve again would keep the earlier approval's commit, and this version would then run
  that commit's image.
- **Agent owners (2026-10-22):** registration now records the registering identity as the
  agent's owner, and only that owner or an admin may delete, restart or upload code to it. Rows
  registered before this have no owner, so after the deploy only admins can change them: set
  owners for agents people still manage (`UPDATE agents SET owner_email = '<email, lowercase>'
  WHERE name = '<name>'`). On a test instance, check that a second account gets 403 on another
  user's agent and that the owner and an admin succeed.
- **Approval lifecycle (2026-10-21 fix set):** changes an operator or admin will notice.
  - The first startup stops what runs for each unapproved row left by earlier versions (an
    upload or reject that kept the status or the service) and resets it to `registered`. On
    Cloud Run this deletes those services. Those agents need a new approval: check that they
    show as pending and that no `agent-<slug>` service is left for them.
  - On the cluster and Cloud Run, an approval without `GITHUB_TOKEN`/`GITHUB_REPO` returns 503
    and starts nothing (before, the cluster spawned directly and Cloud Run deployed `:latest`).
  - On the cluster and Cloud Run, approving an agent that is already approved and `live`,
    `failed` or `stopped` returns 409 (before, it redeployed). Restart it instead, which commits
    again and waits for a CI rebuild.
  - An upload or a reject now stops and unpublishes a running agent.
  - `DELETE /agents/{id}` returns 503 and keeps the row when the directory does not confirm the
    unpublish; `?force=true` (admin only) deletes it anyway.
  - Errors in `llm` and `proxy` handlers end the A2A task `failed`, not `completed`, and the
    `llm` error text changed from "LLM call failed:" to "Agent failed:".
  - The `init_db` purge (`db._PURGEABLE_SQL`) keeps `stopped` and `failed` rows that still hold
    an `ads_cid`.
  - Registration refuses a name whose Cloud Run service id (`agent-<slug>`, sanitized, cut at 63
    characters) matches an existing agent's, such as `weather_bot` next to `Weather Bot`. Check
    that no two production rows already share one.
  - Deploy order on Cloud Run: update `registration` before `chat-app`. The chat app's approval
    view needs the code revision that the new registration service reports; in front of an older
    one it says so and cannot approve. On ENGR one job deploys both from one tree.
- **Tool-loop agents** (`llm` + uploaded tools) now advertise streaming, but only in images
  rebuilt with this skeleton. Of the vendored copies in `agent-directory`, only
  `registered_agents/echo-test` has the tool-loop path, and it still says `streaming=False`.
- **Cluster startup recovery:** on registration startup, `Supervisor.recover_live_agents` now
  re-queues every `live` row whose config uploads code to `image_ready` if its
  `agenticnetwork-custom-<slug>` image is still on Docker Hub. That covers `custom`, `llm` with
  uploaded tools, and legacy `tool_loop` rows. Before, only `handler_type='custom'` was
  re-queued; the others were stopped and then purged by `init_db`. Plain `llm` and `proxy` rows
  are still stopped, since they have no cluster spawn path. After the next ENGR deploy, check
  that any such tool-loop agent comes back `live`, and that its `custom-<id>.log` shows
  `container_runner_tool_loop_start`.
- **Identity in agent prompts:** skeleton-engine agents now keep the user's identity out of
  the model prompt (sprint priority 2). Each per-agent image bakes in `agent_skeleton/` at
  build time, so uploaded tool-loop images built before this change keep sending the user's
  email to their model until rebuilt. The same holds for the vendored skeleton copies in
  `agent-directory/registered_agents/`.
- **dirctl timeouts:** every chat app `dirctl` call is now killed after
  `DIRCTL_TIMEOUT_SECONDS` (default 60 s; before, no limit), and `/ready` kills its probe
  after 4 s. Raise the setting if a slow directory needs longer.
- **Stored agents:** `entry_file` must now be a bare `*.py` name, and stored configs are
  validated when read. All 17 registered agents use `handler.py`; before deploying, check
  that no production row has a nested `entry_file`.
- **Tool schemas (2026-10-15):** agents rebuilt with this skeleton check each tool schema's
  envelope at startup: top-level `"type": "function"`, and `parameters.type` `"object"` when
  parameters are given. A wrong value fails startup; a missing one only logs a warning, so an
  approved upload that omits `"type"` (vLLM tolerates it) keeps starting. Every schema in this
  repo and `agent-directory` passes.
- **Registration UI assets:** the unused `/static` mount is gone (the UI is the single
  page served at `/`).
- **Credential scoping:** agents that declare no credentials now get none (before, the
  whole bundle). The email agent reads `google_oauth` without declaring it, so ENGR's real
  `deployment/.env` must add `PLANNER_UNSCOPED_CREDENTIAL_AGENTS=email-agent,geospatial-agent`
  (as in `.env.example`; ids as listed by `GET /agents`), or the email agent stops getting
  each user's Google token and falls back to its on-disk token file, if any. Check the
  chat app's log (`chat-app.log` on ENGR) for `credentials_unscoped_legacy_agent`. The geo agent
  is listed only to keep today's behaviour: it reads the pre-catalog names
  `google_api`/`google_maps`, which the chat app's DB migration renamed. Cloud Run runs no email or geo agent, so it needs no list.
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
- **Scrub before visibility changes** (audit 2026-10-15):
  - Current tree: the planner prompt's email example, the test fixtures, the `.env` templates
    and the registration UI placeholder now use `example.com` addresses (2026-10-15). If the
    ops docs ship, scrub their maintainer address, GCP project number, internal IPs and
    hostnames, and personal Docker Hub namespace first.
  - History: real-looking personal addresses in the email agent card's examples (from
    `a7786fb` on) and in old copies of `email_agent.py`, a contributor's address in the
    planner prompt (from `908f16d`), three YOLO weight files (about 96 MB), cluster logs, the
    early prototypes, the specialist agents now in `agent-directory`, internal plan documents
    and 14 author identities. No keys or tokens were found. Publish from a fresh repository
    (next bullet), not this history.
  - `agent-directory/email_agent/email_agent.card.json` still holds the two card addresses;
    fix it there too.
- **History for the public copy (audit 2026-10-15):** publish a fresh repository whose first
  commit is the audited release tree, not this repository's history. Filtering the history
  would still leave 14 author identities and unscrubbed commit messages, and cut the history
  of moved files anyway. This repository stays private as the development record; credit
  contributors (with consent) in the README or an `AUTHORS` file. Sketch, to run only after
  the ops-docs decision and from a clean `main`:

  ```bash
  OUT="$(mktemp -d)/agenticnetwork-public"; mkdir -p "$OUT"
  # tracked files only; add one ':(exclude)<path>' per ops doc that moves to a private repo
  git archive --format=tar main -- . | tar -x -C "$OUT"
  cd "$OUT" && git init -b main && git add -A && git commit -m "Initial public release"
  # before adding a remote: no blob over 1 MB, no real addresses, no private paths or IPs
  git grep -nIE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' | grep -viE '@example\.|noreply'
  ```
- **Contributor clearance:** the current tree has lines from seven authors, including
  students, a contributor who committed from a non-WashU address (most of
  `agent_directory_service/`, now merged into `agentnet/`) and a hackathon participant (the metadata filter in
  `agent_skeleton/executor.py`). Confirm under WashU's IP policy, and with the non-WashU
  contributor in writing, that each contribution can be released under the chosen license,
  and record the copyright line.
- **Other decisions before visibility changes (audit 2026-10-15):**
  - The ops docs: about 80 references in 25 shipping files point into them (the README's
    deployment table, `DEPLOY_YOUR_OWN.md`, `SECURITY.md`, `CLAUDE.md`, `docs/README.md`), so
    moving them needs a link pass first.
  - Links to the private `agent-directory` repo (about 9 in shipping docs) 404 for the public:
    publish that repo after its own scrub, or turn the links into plain text.
  - WashU defaults in code: `IMAGE_REGISTRY` defaults to `di2washu`, `CLUSTER_WORKSPACE_MOUNT`
    and the sbatch scripts use `/engrfs` paths, and the landing page says "Sign in with WashU".
    Either label them as the WashU example and document `IMAGE_REGISTRY` as required, or make
    it required for the cluster and Cloud Run backends (then check ENGR's real `.env` sets it).
  - `SECURITY.md` names the live instance next to its open hazards: keep it (none is reachable
    without node access; say so) or describe the hazards generically.

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
   Tier 1 and the native stack are done and pass in CI. Tier 2 (Docker Compose, real Postgres
   and `dirctl`) passed locally on 2026-10-15 (arm64, and emulated amd64 with a conda-free
   planner image) and on GitHub's amd64 runner with the shipped images (`28ab3ce`). Still to do: a tier-3 real-model
   run, `add-agent` for an agent folder, and the scenarios not yet covered (below).
2. **Security fixes that must land before release**, each with a regression test:
   - Fixed: a request-body `user_id` no longer overrides the logged-in identity (planner
     tests and the full-chain test).
   - Fixed: only the conversation owner can answer a pending write confirmation or
     credential consent. `POST /confirmations` returns 403 to anyone else, for confirm and
     cancel (`test_confirmations_forbid_non_owner`), and so does a chat "yes" or "no" naming
     the owner's `context_id`. That reply used to resume the action with the replier's
     credentials and record a consent grant under the replier's account (fixed 2026-10-15;
     `test_confirmation_ownership.py`, pipeline `test_8`). Both paths use one check
     (`runtime._owned_context`), which falls back to the pending row's owner when the history
     save failed, so the earlier cancel gap on another process is closed. A pending action
     resumed or cancelled on one planner process is no longer run again from another
     process's in-memory copy: with a database, the pending row is read on every lookup.
   - Fixed: two answers to the same pending action arriving at once (a double click, a client
     retry, a chat "yes" racing the button, replies on two processes) both ran the write,
     because each read the action, sent the call and cleared it only afterwards. Now one
     answer per conversation runs at a time in a process, and an answer claims the action
     right before the agent call (with a database one `UPDATE` marks the row claimed and it is
     deleted once the answer is done; without one it takes the process's in-memory copy), so
     only one answer acts; the others get `answer_in_progress` or `no_pending` and nothing
     runs. A claim the database can't serve runs nothing, and the resumed call sends the
     request the prompt showed (fixed 2026-10-15; `test_confirmation_claim.py`, pipeline
     `test_9`; two adversarial review rounds).
   - Fixed: a chat "yes" reaching another planner process during the first answer's agent call
     found nothing pending (the claim had deleted the row) and was handled as an ordinary
     message, so the model could call the agent again. The claimed row now stays until the
     answer is done (refreshed while the call runs, deleted even when the chat stream closes
     mid-call), so that answer gets `answer_in_progress`; a claim never finished (its process
     died) expires after 2 minutes and is never claimed again. And a confirm named no
     prompt, so a click on an old prompt answered a newer one saved for the same conversation
     meanwhile; each prompt now has an id that the button sends back, and a click on a replaced
     prompt runs nothing (`superseded`) (fixed 2026-10-16; `test_confirmation_claim.py`,
     `test_pending_store_postgres.py` on a real Postgres 15, pipeline `test_7`; three
     adversarial review rounds, whose confirmed findings are fixed: the delete survives a
     closed stream, claims carry an id so only their own answer refreshes, releases or deletes
     them, each refresh is bounded and drops a dead connection, and a claim whose statement
     failed or was cancelled is released, even one the database applies only later).
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
   - Fixed (2026-10-15, from a contribution by raneemousa3, agent-skeleton PR #29): skeleton-engine
     agents strip `credentials`, `authorization`, `secret(s)` and `token` keys from request,
     message and part metadata and DataPart data before the LLM prompt
     (`agent_skeleton/executor.py::_model_visible_metadata`; `agent_skeleton/tests/test_executor_metadata.py`).
     Registration's runner no longer logs a bad config's values (`registration_service/tests/test_prompt_secrets.py`).
     Fixed (2026-10-15): they also drop the user's identity (`user_id`, `auth_user_id`, `mail`,
     `eppn`, `remote_user`/`remote-user`) from request, message and part metadata, so the email
     no longer reaches the model provider. This is a denylist, not an allowlist, because the
     planner spreads task input into metadata and direct callers send structured input there.
     Other metadata (channel, skill hints, task input, `user_goal`, uploaded file names) still
     reaches the model; DataPart data loses only the credential keys
     (`test_executor_metadata.py`, which also drives the cluster/Cloud Run tool-loop image's
     app; the planner side is pinned by `test_login_identity_rides_only_in_request_metadata`).
     Registration's local `llm`, http-operation and `proxy` agents send only the user's text.
     Images built before this fix keep the old executor until rebuilt (see the checks above).
   - Fixed: user bug reports no longer default to this repo. `GITHUB_ISSUES_REPO` has no
     default; unset, `POST /issues` returns 502 without calling GitHub. Issue bodies still
     include the reporter's email, prompts and trace, so the repo must be private
     (`test_issues_endpoint.py`: `test_create_issue_requires_repo`,
     `test_issues_without_repo_502_and_no_github_call`).
3. **CI:** done. `.github/workflows/tests.yml` runs the unit suites (planner, registration,
   skeleton, ADS, devstack), repo hygiene, the native full-chain test and the scenario tests;
   it first passed on GitHub for `d069aaf` (2026-10-15). `compose.yml` (tier 2) runs on PRs and
   pushes to `main` that touch service or deployment code, weekly and on demand.
4. **Hygiene:** done. The tracked `.gitignore` covers `build/`, `dist/`, `Local/`, `.env*`
   (except examples), keys, model weights and the dev-stack state dirs; CI's hygiene job
   checks it. 2026-10-15: test fixtures, `.env` templates and the registration UI use
   `example.com` identities (`example-project`, `example-org/agents` in tests); code comments
   no longer cite the retired plan's D- and §-numbers; Compose images are named per project.
5. **Merge the agent_skeleton copies** and bring the standalone history in with
   `git subtree` (see "Before the subtree merge"). Audit 2026-10-15: no standalone-only fix
   needs the history merge. The six real bugs fixed only in the standalone copy are now
   ported into `agent_skeleton/`, each with a test (`tests/test_contract.py`,
   `test_preset_tools.py`, `test_wrapper.py`): `e40580d` tool-schema envelope check (here
   `parameters.type` is checked only when `parameters` is non-empty, and a missing `type`
   only warns, so no-argument tools written as `{}` and uploads that omit `type` keep
   working), `579996b` redacting quoted-key secrets in JSON error bodies, `3de317f`
   calculator rejecting complex/inf/nan, `6e4d371` AgentHandler `files=None`
   defaults and the null-safe credentials example, `3e1dc8c` a clear `AGENT_A2A_PORT` error,
   `ce70bdc` the `[http-server]` install hint. The standalone `KNOWN_ISSUES.md` #3–#6 are
   open in both copies. The in-repo copy can now ship as is; the subtree merge would add only
   the standalone history and its layout, and whether to do it is Ronak's call.

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
| 1. Native (done) | Every PR | Chat app, registration service and skeleton agents as local processes (`scripts/stack`); mock LLM; in-memory DB; fake `dirctl` |
| 2. Containers (done locally) | PRs and pushes to `main` touching services or deployment, weekly (`compose.yml`) | The service images, real Postgres and the real `dirctl` daemon via Docker Compose (`deployment/compose/smoke.sh`); mock LLM |
| 3. Real LLM | Manual or nightly, opt-in | One round trip with a hosted or local model |

**Covered by tier 1** (`tests/pipeline/test_full_chain.py`, driving `scripts/stack`): start
from nothing; the first signed-in request creates the user; a prompt reaches the seed
agent and the answer is saved for that user only; streaming; a newly registered `llm`
agent receives the prompt that matches it; a body `user_id` is ignored when a login
identity exists; `down` leaves nothing running. `tests/pipeline/test_scenarios.py` adds: a
declaring agent receives only its scoped credentials and an undeclared one none; an
unreachable agent (refused or silent) yields a clear error within the A2A timeout; after
`down` and `up`, a registered agent is listed once and answers; a `proxy` agent registered
through `add-agent` relays to its upstream; an `llm` agent with the preset `calculator` calls
it; the step limit ends a turn that never answers (`max_steps_exceeded`); `ask_user` pauses a
turn and the reply continues it with the history; in strict policy mode a write skill runs
only after its owner confirms (another user gets 403; a chat "no" cancels it), and another
user's chat "yes" or "no" does not touch the owner's pending action; two confirmations sent
together run the write once. Unit tests cover the
refused `entry_file` and impersonating uploads.

**Still to add:**

- Each handler type: `llm` with HTTP tools and uploaded tools, `custom` upload (presets and
  `proxy` are covered). Legacy type names normalize on read.
- An agent built on the skeleton runtime, through each path (the generator's endpoint
  wrapper, `llm` with uploaded tools, `custom`), works end to end.
- Confirmation and `ask_user` resumed on another chat app process or after a chat app restart
  (needs Postgres, so tier 2).

Security scenarios that fail today are added as strict expected failures and flipped
when their fix lands.

## Deferred simplifications

Bigger or breaking cuts found in the 2026-10-13 review, kept for later. Sizes are rough
lines removed or rewritten.

| Simplification | ~Lines | Waits on |
|---|---|---|
| **Protocol and directory** | | |
| One shared directory/A2A package: done 2026-10-20 (`agentnet/` replaced the chat app's copy, `agent_directory_service/` and `ads_utils/`). Still open: dedupe the admin check and the credential catalog | — | — |
| agentnet's `ADS_BACKEND=sdk` path and the ADS `agntcy-dir`/buf.build deps (unused today) | 100 | how the GCP directory client gets built |
| Collapse the ADS address aliases into `ADS_SERVER_ADDR` | 20 | a coordinated `agent-directory` change and cluster `.env` update |
| **Skeleton subtree and runner merge** | | |
| The `git subtree` merge | 1,500 | "Before the subtree merge" above |
| Merge registration's `runner.py` + `handlers/` with `container_runner.py`; one A2A app factory; isolate the ENGR-only code | 580 | the subtree merge; a stable full-chain test |
| Drop the generator (with `repo_emitter`'s unused `import os`); make `AgentSpec` mandatory and drop the demo lane | 1,030 | the generator and template decisions at the merge |
| The multi-endpoint manager | 260 | wire it or drop it (above) |
| Refresh the 17 vendored skeleton copies in `agent-directory/registered_agents/` | 2,200 | a re-vendor pass, or the subtree merge |
| **Chat app and planner structure** | | |
| Split `create_app` (nested routes) into routers with explicit app state | 600 | an agreed layout |
| Move the email-specific heuristics out of the generic loop | 150 | cards declaring input fields and a write tag |
| Run `dirctl` off the event loop (calls are now time-bounded) | 40 | a cache TTL and concurrency tests |
| Drop the `channel` field everywhere | 40 | a written A2A metadata contract |
| Retire `chat_app/scripts/doctor.sh` | 150 | `scripts/stack doctor` covering real ADS and LLM reachability |
| **Registration internals** | | |
| Retire the legacy handler-type names and config shapes, with `container_runner`'s legacy envelope | 230 | a one-time DB migration on ENGR and GCP |
| Put all SQL behind `db.py` helpers, then replace `fake_db.py`'s SQL matching | 260 | one typed pool-exhausted error; a tier-2 Postgres test |
| Drop `build_manifest`'s dict-config support | 25 | typed test fixtures |
| **Dependency and version upgrades** | | |
| `dir`/`dirctl` v1.2 → v1.7, then delete the stored-but-not-routed workarounds (agentnet's backend, ADS repair/watch tooling) | 1,550 | re-validating publish and discovery on ENGR and GCP |
| One `a2a-sdk` line (0.3.26 everywhere, then 1.x), replacing the deprecated `A2AClient` and the skeleton's import guard | 270 | the full-chain test on the new pins; rebuilt agent images |
| Test-only deps into `requirements-dev.txt`; the chat app's pip freeze becomes direct deps plus a lock file | 50 | a locking tool, with CI and docs updated together |
| Pin `registration_service/requirements.txt` (only minimums today, so a fresh clone gets e.g. openai 3.x where the tested venv has 2.x; all suites passed with both on 2026-10-15), or add a constraints file | 40 | the same locking tool; a registration image rebuild |
| **Images, deploy config and hygiene** | | |
| Chat app image (`Dockerfile.chat-app`) on `python:3.x-slim` instead of miniconda; one Python version across images | 45 | an ENGR test deploy and a Cloud Run staging deploy |
| One ADS image tag (`submit.sbatch` pulls `:v4`, `build_and_push.sh` pushes `:latest`) | 2 | confirming they match |
| Generate `ads_daemon.gcp.yaml` from `ads_daemon.yaml` | 70 | the GCP runbook and VM startup |
| Prune cluster- and person-specific `.gitignore` rules; dedupe the secrets rules | 16 | checking the ENGR checkout |
| Delete the untracked `Local/` laptop stack | — | Ronak (it holds local secrets) |
| **Docs restructure** | | |
| Fold `CLAUDE.md` into `CONTRIBUTING.md`, `ARCHITECTURE.md` and the deployment docs; keep a short agent entry file | 450 | an agreed outline; a `§`-reference checker |
| Move `agent-directory`-specific content (specialist envs, LLM matrix, caveats, hand-built cluster path) to that repo | 540 | a docs home there |
| Replace `agent_skeleton/README.md` and `CLAUDE.md` with the standalone repo's docs | 365 | the subtree merge |
| Rewrite the protocol-duplication docs: done for this repo 2026-10-20 (`SECURITY.md` §3 lists the copies left, mostly in `agent-directory`) | — | — |
| `SECURITY.md` becomes a public policy; this file becomes a roadmap/changelog or goes | 260 | the security fixes; release sign-off |
| A docs link and `§`-reference check in CI | 60 | the doc fixes landing |
| Stale docs in the sibling repos: `agent-directory/ads_utils/README.md`; `agent-skeleton/CARD_AUTHORING_PLAN.md` (converter cleanup; a card path now under `chat_app/tests/fixtures/agent_cards/`); `KNOWN_ISSUES.md` (a lab IP) | 6 | a session in those repos |
| **Ops docs scope** | | |
| Ship or move the WashU ops docs (`docs/deployment/engr`, `gcp/*`, `DOWNSTREAM`, `MULTI_CONTRIBUTOR`, `TESTING_CHANGES`) | 3,300 | Ronak and the PIs/DI2; then scrub or move, and filter history |
| Merge the three ENGR test-instance docs; consolidate the GCP design and progress logs | 850 | the decision above; resuming GCP work |
