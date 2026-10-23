# Registration Service

`registration_service/` — a FastAPI app (`:8091`) + static web UI (the sign-up wizard) that
registers agents into the network and then runs them: it spawns them (building an image first on
the cluster and Cloud Run), publishes their card to ADS, and monitors health. People reach it at
`/register/` on the chat app's address, which passes those requests through. It is the on-ramp for
**user-uploaded custom agents** and the home of the approval trust boundary.

Tests: `registration_service/.venv/bin/python -m pytest registration_service/tests -q -p no:cacheprovider`
(conftest sets `FAKE_DB=1`). Whole system locally: `scripts/stack up` ([DEVSTACK.md](DEVSTACK.md)).

## Handler types

An agent's `handler_type` (stored in the `agents` row) selects **who runs the loop**.
There are three, consolidated from five on 2026-10-10 ([DECISIONS.md](DECISIONS.md) D13):

| Type | Config (`models.py`) | Runs as | Approval? |
|---|---|---|---|
| `llm` | `LLMConfig` (system_prompt, model, **tools**) | `LLMAgentExecutor` over an `AgentSpec` built from `tools` | auto, **unless** `tools.source == "upload"` |
| `proxy` | `ProxyConfig` (upstream_url, auth_header) | `ProxyExecutor` — forwards the A2A task to an upstream HTTP/JSON-RPC service; no LLM | auto |
| `custom` | `CustomConfig` (class_name, entry_file, requirements, system_packages, credentials, …) | a user-uploaded `AgentHandler` subclass run via the skeleton `HandlerExecutor` | **admin** |

For all three, an error while handling a request (the handler raises, the model call fails, the
upstream request fails or the upstream reports an error or a failed task) ends the A2A task
`failed`, with the error text as its message.

`LLMConfig.tools` is a discriminated union on `source` — `none` (a single turn),
`presets` (built-in catalog), `upload` (author-uploaded tool bodies), `http` (one typed
tool per API operation), `raw` (schemas with no bodies; runs prompt-only). This axis
used to be three separate handler types: `tool_loop` is now `upload` and
`endpoint_ops` is now `http`, because neither was ever a different *runtime* from
`llm` — their handlers (now `llm_upload_handler.py` and `llm_http_handler.py`) reuse
`llm_handler`'s executor and card builder verbatim; only the spec differed.

`runner.py` dispatches on the canonical type and, for `llm`, on `tools.source`.
The **approval gate is a property of the config, not the type name**:
`models.carries_upload(config)` is true for `custom` and for `llm` + `upload`, and
those are the agents that are staggered (register → upload → approve → spawn).
Everything else spawns immediately on `POST /agents`.
`models.upload_spec(config)` normalizes both uploaded-code shapes into one set of
build parameters, so a single `generate_dockerfile` serves both.

**Legacy names** (`api_proxy`, `tool_loop`, `endpoint_ops`) and their old flat config
shapes are still accepted and still parse; a registration under a legacy name is
persisted under the canonical one. Existing rows keep their old `handler_type` *and*
old config JSON and are normalized **on read** — never rewritten — so
`agents_handler_type_check` permits both sets.

## HTTP surface (`server.py`)

- **Public:** `GET /health`, `GET /ready` (DB + directory; 503 if either errors), `GET /` (UI),
  `GET /oasf-skills`, `GET /credential-catalog`.
- **Agent CRUD:** `POST /agents` (register; any signed-in user, whose SSO identity is recorded
  as the agent's `owner_email`), `GET /agents`, `GET /agents/{id}`. For the agent's owner or an
  admin only (`_require_owner_or_admin`, 403 otherwise): `DELETE /agents/{id}` (stop + unpublish
  + delete + clean handler dir; if the directory does not confirm the card's removal, 503 and the
  stopped row is kept so the delete can be retried; `?force=true`, admin-only, deletes it anyway
  and logs the CID of the card left in the directory) and `POST /agents/{id}/restart`. An agent
  with no owner (registered without an identity, or before owners were recorded on 2026-10-22)
  is admin-only. Responses show secret `config` values as `[redacted]` (a proxy's
  `auth_header`, the values of `extra` and `tools.extra`, credentials in a URL;
  `server.py::_public_config`) and leave out the owner; runtimes read the stored config.
- **Custom upload (owner or admin):** `POST /agents/{id}/handler`.
- **Custom review/approval (admin-gated by `_require_admin`):** `GET /agents/{id}/handler`
  (review source; also names the owner), `GET /agents/{id}/handler/download`,
  `POST /agents/{id}/approve`, `POST /agents/{id}/reject`.
- **Internal (ENGR host spawn loop):** `GET /internal/spawn-queue`, `POST /internal/spawned`,
  `GET /internal/stop-queue`. No access check in code: they rely on `:8091` not being reachable
  from outside, and the chat app's `/register` pass-through refuses them (see [`SECURITY.md`](../SECURITY.md) §2).

Admin auth (`_require_admin`) is the SSO-identity `ADMIN_EMAILS` allowlist; the owner check
(`_require_owner_or_admin`) compares the same forwarded identity with the row's `owner_email`.
Both are dev-open under `FAKE_DB=1` — see [AUTH.md](AUTH.md) §2.

## Data model (`db.py`)

- **`agents`** — `id, name, description, handler_type, oasf_skills (jsonb), config (jsonb), port
  (unique), status, handler_file_path, approved, is_archive, endpoint_url, ads_cid, image_ref,
  build_commit, resources, owner_email, created_at, updated_at`. `status ∈ {registered, live, failed, stopped,
  building, image_ready}`. `build_commit` is the `agent-directory` commit an approval's build
  context went into; `image_ref` is the digest of the image tagged with that commit.
  `owner_email` is the SSO identity that registered the agent (lowercased; none if the request
  had no identity); no API changes it, and an admin hands an agent over with
  `UPDATE agents SET owner_email = '<email>' WHERE id = '<id>'`.
- **`port_pool`** — `port, in_use, agent_id`. `port_pool.assign_port` picks a free port
  (19200–19299) with `FOR UPDATE SKIP LOCKED`; `release_port` frees it.
- **`FAKE_DB=1`** swaps in an in-memory `FakePool`/`FakeStore` (`fake_db.py`) and a no-op
  `FakeSupervisor` (`fake_supervisor.py`) — used by dev mode and the test suite.
  `FAKE_DB=1 FAKE_SUPERVISOR=0` keeps the in-memory DB but runs the real supervisor (the dev stack).

## Lifecycle

**Auto (any config that ships no code — `proxy`, and `llm` with tools.source
`none`/`presets`/`http`/`raw`):** `POST /agents` → insert (`approved=TRUE`) → `assign_port` →
`supervisor.spawn_and_publish` → backend spawns → wait until it serves its agent card → build card
(`_build_agent_card`) → `publisher.publish(card)` sets `ads_cid` → `status='live'`.

**Uploaded code (staggered — `custom`, and `llm` with tools.source `upload`;
`models.carries_upload(config)` is the gate):**
1. `POST /agents` → insert `approved=FALSE`, `status='registered'`. A name is refused (409) when
   its slug (lowercased, spaces to hyphens) or its Cloud Run service id (`agent-<slug>`, further
   sanitized and cut at 63 characters) matches an existing agent's, on every backend.
2. `POST /agents/{id}/handler` → write the upload (a `.py` entry file, or a `.zip` unpacked to a
   tree — detected by suffix or PK magic) to a staging directory and AST-validate the entry file
   (a bare `*.py` name; paths are refused at registration). A rejected upload (422) leaves the
   stored files and the row unchanged. An accepted one replaces `HANDLER_FILES_DIR/{id}/` by
   rename and resets the agent to the pre-approval state: `approved=FALSE`, `status='registered'`,
   `image_ref` and `build_commit` cleared. A re-upload of a running, published or building agent therefore stops and
   unpublishes it: it is offline until an admin approves the new code, and that approval goes
   through the normal build path.
3. Admin reviews via `GET /agents/{id}/handler` (source/manifest + warnings + declared
   `system_packages` + declared `credentials` + `revision`). The revision is a sha256 over the
   relative paths and bytes of the files the manifest and download show (`archive.tree_revision`
   over `list_tree`). An archive's members under `.venv/`, `__pycache__/` or `.git/` are not
   extracted (the upload response says how many were skipped; they still count toward
   `MAX_ARCHIVE_FILES`): those are the directories the local
   backend creates (its venv and bytecode cache) and that the manifest, revision and build skip, so
   the reviewed files are all of the stored code.
4. `POST /agents/{id}/approve` → `approved=TRUE` → `spawn_and_publish`. The admin UI sends
   `{"revision": ...}` with the revision of the code it showed, and careful callers should too: if
   the stored code no longer has it (an upload landed after the review), nothing is approved and
   the call returns 409. A call without a body approves the code stored when it runs. The admin
   UI's code viewer says so when the code changed since it last showed it, and Approve then waits
   until the viewer is opened again (for an archive, after downloading it again). An agent
   that is already approved and `building` or `image_ready` is not approved again (409). An
   unapproved agent first gets the pre-approval reset of step 5, which stops what may still run
   for it (left by a revoke, delete or startup stop that did not finish, or by an earlier
   version). A launch the backend refuses leaves the approval as it was: 409 for the agent's
   state, 503 for missing build credentials (below).
   - **Cluster build** (`SPAWN_BACKEND=cluster` + `GITHUB_TOKEN` + `GITHUB_REPO`): commit a
     generated Dockerfile + card + the uploaded tree + vendored `agent_skeleton/` to GitHub under
     `registered_agents/{slug}/`; the row records the commit (`build_commit`) and goes
     `status='building'`; a build loop waits for the image tagged with that commit's SHA, records
     its digest (`image_ref`) and sets `image_ready`; the host loop
     (`deployment/spawn_agent_loop.py`) polls `/internal/spawn-queue`, runs `srun --overlap` on that
     commit's tag, and reports the PID to `/internal/spawned` → `finalize_spawned` publishes
     + `status='live'`. The commit replaces the whole directory: a file an earlier approval
     committed that the new upload no longer has is deleted, since the image build copies the
     directory. Each approval's commit also writes `registered_agents/{slug}/.approval`
     (approval time and code revision), so it always changes the tree and the image is rebuilt,
     also for unchanged code (a reject then approve, or a re-upload of the same files).
   - **Cloud Run** (`SPAWN_BACKEND=cloudrun`): the same GitHub commit and image build; once the
     image is ready the supervisor calls `CloudRunBackend.spawn` itself (Cloud Run Admin API, no
     host loop) to deploy a scale-to-zero service from that image's digest (the commit's tag if
     the digest lookup failed), and the card is published so it stays
     discoverable at zero instances. A deploy that fails deletes the service it may have created
     and sets the agent `failed`. See
     [`docs/deployment/gcp/README.md`](deployment/gcp/README.md).
   - On these two backends uploaded code runs only from the image built for its approval. Approving
     an approved agent that is not waiting for its first build (`live`, `failed`, `stopped`) starts
     nothing and returns 409; restart it instead, which sends it through the build again. Without
     `GITHUB_TOKEN` and `GITHUB_REPO` an approval returns 503 and the agent stays unapproved. The
     service waits for the image without a time limit, so a build that fails (its log is in the
     agent-directory repository's Actions) leaves the agent `building`: restart it to build again,
     or upload fixed code, which returns it to approval.
   - The agent runs the image built from its own approval's commit, never whatever `:latest`
     holds (an older build that finished last, or another approval's). This needs the build
     workflow in `GITHUB_REPO` to tag each image with the commit's SHA as well as `:latest`
     (`agent-directory`'s `build-custom-agent.yml` does, from 2026-10-22). With a workflow that
     pushes only `:latest`, new approvals stay `building`, so update the workflow before deploying
     this service; an agent approved while the old workflow ran stays `building` after the update
     (that commit was built without the tag), so restart it. A row that was already `building`
     without a recorded commit still waits for a `:latest` pushed after its approval, and one
     without a commit launches `:latest`. Before running an older version of the service
     against the same database, run `UPDATE agents SET build_commit = NULL`: older versions
     neither record nor clear the column, so a row they approve again would keep the earlier
     approval's commit, and a later return to this version would run that commit's image.
   - **Local** (`SubprocessBackend`): spawn a subprocess directly (venv + `pip install` if
     `requirements`/`install_project`; the project is installed from a temporary copy, since pip
     builds in the directory it installs from and the stored tree must stay the reviewed code),
     `PYTHONPATH` = handler dir + repo root, `HANDLER_FILE_PATH`
     set; wait until it serves `/.well-known/agent-card.json`; publish; `status='live'`.
5. `POST /agents/{id}/reject` → the same pre-approval reset as a re-upload: an agent that may
   still run or be published (any status but `registered` or `stopped`, or one that holds an image
   or a card) is stopped and unpublished, a queued build or spawn is dropped, and a launch already
   in progress is stopped (a publish under way finishes first, then its card is removed); reason
   logged. That stop also retries one that failed before (a Cloud Run delete that failed after a
   failed deploy, or at startup).

Revoked work is not launched later: the build poll moves a row to `image_ready` only if it is
still the approved `building` row it listed, the spawn queue hands the host loop only approved
rows, a cluster srun that reports back after a revoke is stopped, and a Cloud Run deploy that
gets the agent's lock after a revoke does not run. The revoke has already returned the row to
`registered`; one that was approved again in the meantime keeps waiting for its new build.

Registration, upload, source review, approve, reject, restart and delete of one agent run one at a
time (a per-agent lock, `supervisor._agent_lock`, keyed by the canonical form of the agent's UUID,
so every spelling of the id names the same agent, lock and handler directory). The supervisor's
launches take the same lock: a health-loop restart attempt, the build poll's move to `image_ready`,
a Cloud Run deploy and the cluster host loop's report of a started process. A restart therefore
starts from the files that were approved, and a reject, upload or delete that arrives during a
launch waits for it, then stops the agent and removes the card it published. A launch reported for
an agent that was deleted in the meantime is stopped (on the cluster, its PID goes to the stop
queue). The cluster records a reported process before it publishes, and the spawn queue leaves out a
row with a recorded process, so the host loop does not launch an `image_ready` row again while the
report of its first launch is still publishing (a second process would compete for its port); a
report that still comes for a row already live is stopped. If that publish fails with anything but a
directory outage (an invalid card, a database error), the reported process is stopped and the agent
set `failed`. Such a wait can outlast the chat app's 30 s pass-through timeout (a Cloud Run deploy,
bounded by `CLOUDRUN_OPERATION_TIMEOUT_SECONDS`, a local spawn's install steps, each bounded by
`AGENT_INSTALL_TIMEOUT_SECONDS`, and health wait, bounded by `AGENT_SPAWN_HEALTH_TIMEOUT_SECONDS`, a
build backend's commit of the approved code to GitHub, or directory calls, each bounded by
`DIRCTL_TIMEOUT_SECONDS` and made one at a time across agents): the chat app then answers 504, and
the registration service still completes the request. If the request did not reach the service in
time (no connection, or the pass-through's connection pool was full), the chat app answers 503 and
nothing was changed.

Health: the local backend checks each agent's process and restarts it with backoff (≤3 attempts,
then `failed`); an attempt does nothing if the agent was meanwhile deleted, stopped, rejected or
sent back through the build, or is running again. An agent still `live` without approval (a revoke
whose stop failed) gets the pre-approval reset instead. Startup recovery does the same on every
backend for each unapproved row that may still run (any status but `registered` or `stopped`, or
`registered` with an image, as earlier versions' upload and reject left them): it is stopped,
unpublished and set `registered`, not kept live or relaunched. A stop that fails leaves the row for
the next startup or a reject. The cluster and Cloud Run lifecycles are owned by Slurm and Cloud Run
(cluster agents are not health-checked; restart one to redeploy it). On those two backends
prompt-only `llm` and `proxy` agents have no spawn path and are marked `failed`. Stop unpublishes
from ADS, kills the process, releases the port, and cancels a pending health-loop restart. If the
directory does not confirm the unpublish, the row keeps `ads_cid` (a CID the directory reports as
"record not found" counts as removed). A publish that the directory stored but did not show in its
routing index in time also leaves its CID on the row, since the card may be routed later. The
startup purge spares rows that hold one, and startup recovery retries the unpublish and clears the
CID once it succeeds. It first probes the directory and skips the retry if the directory does not
answer, so an outage does not delay startup. An approved row still `building` keeps its CID until it
publishes or is stopped (for a row with no recorded commit, clearing it would move `updated_at`,
which the build poll compares a `:latest` image against). On the cluster the registration step starts only with the Slurm job, after the
directory's database and container are recreated, so recovery just clears the CIDs. A later publish
for the agent also retries removing a card the row still records. Stopping a Cloud Run agent whose
service no longer exists succeeds.

## The custom-agent trust boundary (D2)

Uploaded Python is **not sandboxed by the validator.** `validator.py` is pure-AST and never
executes code: it *blocks* only structural problems (syntax error, missing class, class not
subclassing `AgentHandler`, missing `handle_structured`), and `subprocess`/`eval`/`os.system`/socket
imports produce **advisory warnings** shown to the approver — not blocks. Only the *entry file* is
scanned, not the whole tree. `system_packages` (apt, cluster image only) are charset-sanitized
against shell injection (`_APT_TOKEN_RE`) but are not an allowlist.

**The real containment is admin approval + per-agent container isolation** — which is exactly why
`custom` agents start un-approved. See [`SECURITY.md`](../SECURITY.md) §2.

## Card assembly + credential declaration (P4a)

`supervisor._build_agent_card` (and the served-card builder in `handlers/custom_handler.py`) builds
the `AgentCard` from `oasf_skills` + port, sets `capabilities.streaming=True`, and — if the config
declares `credentials` — appends the credential extension via
`card_credentials.py::build_credentials_extension` under
`agenticnetwork.dev/ext/required-credentials`. The planner reads that back (see [AUTH.md](AUTH.md)
§4). The credential catalog (`credential_catalog.py`) is the source of valid `type` values, mirrored
in the chat app (`chat_app/credential_catalog.py`).

## Key files
| Path | Role |
|---|---|
| `server.py` | routes, admin gate, upload/approve flow |
| `db.py` / `fake_db.py` | schema + pool; in-memory dev/test swap |
| `supervisor.py` / `fake_supervisor.py` | spawn/publish/health lifecycle; GitHub commit on approval |
| `backend.py` | `SubprocessBackend` (local) / `ClusterBackend` (srun) / `CloudRunBackend` (Cloud Run) |
| `runner.py` | dispatch by canonical type + `tools.source` |
| `handlers/custom_handler.py` | wires an uploaded handler into `HandlerExecutor` + serves it |
| `validator.py` | AST-only upload check (advisory) |
| `port_pool.py` | port allocation |
| `models.py` | `RegisterRequest`, `CustomConfig`, `CredentialRequirement`, `AgentResponse` |
| `card_credentials.py` / `credential_catalog.py` | credential declaration on the card |
| `static/index.html` | registration web UI |

See [BUILDING_AGENTS.md](BUILDING_AGENTS.md) for the agent author's perspective.
