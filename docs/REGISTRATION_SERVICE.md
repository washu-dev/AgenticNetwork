# Registration Service

`registration_service/` — a FastAPI app (`:8091`) + static web UI that registers agents into the
network: it spawns them, publishes their card to ADS, and monitors health. It is the on-ramp for
**user-uploaded custom agents** and the home of the approval trust boundary.

Run/dev: `FAKE_DB=1 PYTHONPATH=. pytest registration_service/tests` (see [`CLAUDE.md`](../CLAUDE.md) §12).

## Handler types

An agent's `handler_type` (stored in the `agents` row) selects **who runs the loop**.
There are three, consolidated from five in 2026-07:

| Type | Config (`models.py`) | Runs as | Approval? |
|---|---|---|---|
| `llm` | `LLMConfig` (system_prompt, model, **tools**) | `LLMAgentExecutor` over an `AgentSpec` built from `tools` | auto, **unless** `tools.source == "upload"` |
| `proxy` | `ProxyConfig` (upstream_url, auth_header) | `APIProxyExecutor` — forwards the A2A task to an upstream HTTP/JSON-RPC service; no LLM | auto |
| `custom` | `CustomConfig` (class_name, entry_file, requirements, system_packages, credentials, …) | a user-uploaded `AgentHandler` subclass run via the skeleton `HandlerExecutor` | **admin** |

`LLMConfig.tools` is a discriminated union on `source` — `none` (a single turn),
`presets` (built-in catalog), `upload` (author-uploaded tool bodies), `http` (one typed
tool per API operation), `raw` (schemas with no bodies; runs prompt-only). This axis
used to be three separate handler types: `tool_loop` is now `upload` and
`endpoint_ops` is now `http`, because neither was ever a different *runtime* from
`llm` — `tool_loop_handler.py` reuses `llm_handler`'s executor and card builder
verbatim; only the spec differed.

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

- **Public:** `GET /health`, `GET /` (UI), `GET /oasf-skills`, `GET /credential-catalog`.
- **Agent CRUD (public):** `POST /agents` (register), `GET /agents`, `GET /agents/{id}`,
  `DELETE /agents/{id}` (stop + unpublish + delete + clean handler dir),
  `POST /agents/{id}/restart`.
- **Custom upload/approval (admin-gated by `_require_admin`):** `POST /agents/{id}/handler`
  (upload), `GET /agents/{id}/handler` (review source), `GET /agents/{id}/handler/download`,
  `POST /agents/{id}/approve`, `POST /agents/{id}/reject`.
- **Internal (localhost, host spawn loop):** `GET /internal/spawn-queue`, `POST /internal/spawned`,
  `GET /internal/stop-queue`.

Admin auth (`_require_admin`) is the SSO-identity `ADMIN_EMAILS` allowlist (dev-open under
`FAKE_DB=1`) — see [AUTH.md](AUTH.md) §2.

## Data model (`db.py`)

- **`agents`** — `id, name, description, handler_type, oasf_skills (jsonb), config (jsonb), port
  (unique), status, handler_file_path, approved, is_archive, endpoint_url, ads_cid, created_at,
  updated_at`. `status ∈ {registered, live, failed, stopped, building, image_ready}`.
- **`port_pool`** — `port, in_use, agent_id`. `port_pool.assign_port` picks a free port
  (19200–19299) with `FOR UPDATE SKIP LOCKED`; `release_port` frees it.
- **`FAKE_DB=1`** swaps in an in-memory `FakePool`/`FakeStore` (`fake_db.py`) and a no-op
  `FakeSupervisor` (`fake_supervisor.py`) — used by dev mode and the test suite.

## Lifecycle

**Auto (any config that ships no code — `proxy`, and `llm` with tools.source
`none`/`presets`/`http`/`raw`):** `POST /agents` → insert (`approved=TRUE`) → `assign_port` →
`supervisor.spawn_and_publish` → backend spawns → wait for `/health` → build card
(`_build_agent_card`) → `publisher.publish(card)` sets `ads_cid` → `status='live'`.

**Uploaded code (staggered — `custom`, and `llm` with tools.source `upload`;
`models.carries_upload(config)` is the gate):**
1. `POST /agents` → insert `approved=FALSE`, `status='registered'`.
2. `POST /agents/{id}/handler` → store the upload under `HANDLER_FILES_DIR/{id}/` (a `.py` entry
   file, or a `.zip` unpacked to a tree — detected by suffix or PK magic), AST-validate the entry
   file, keep `approved=FALSE`.
3. Admin reviews via `GET /agents/{id}/handler` (source/manifest + warnings + declared
   `system_packages` + declared `credentials`).
4. `POST /agents/{id}/approve` → `approved=TRUE` → `spawn_and_publish`:
   - **Cluster build** (`SPAWN_BACKEND=cluster` + `GITHUB_TOKEN` + `GITHUB_REPO`): commit a
     generated Dockerfile + card + the uploaded tree + vendored `agent_skeleton/` to GitHub under
     `registered_agents/{slug}/`; `status='building'`; a build loop waits for the image
     (`image_ready`); the host loop (`deployment/spawn_agent_loop.py`) polls `/internal/spawn-queue`,
     runs `srun --overlap`, and reports the PID to `/internal/spawned` → `finalize_spawned` publishes
     + `status='live'`.
   - **Local** (`SubprocessBackend`): spawn a subprocess directly (venv + `pip install` if
     `requirements`/`install_project`), `PYTHONPATH` = handler dir + repo root, `HANDLER_FILE_PATH`
     set; wait for `/health`; publish; `status='live'`.
5. `POST /agents/{id}/reject` → `approved=FALSE`, `status='registered'`, reason logged.

Health: the local backend polls `/health` and restarts with backoff (≤3 attempts, then `failed`);
the cluster lifecycle is owned by Slurm. Stop unpublishes from ADS, kills the process, releases the
port.

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
in the planner.

## Key files
| Path | Role |
|---|---|
| `server.py` | routes, admin gate, upload/approve flow |
| `db.py` / `fake_db.py` | schema + pool; in-memory dev/test swap |
| `supervisor.py` / `fake_supervisor.py` | spawn/publish/health lifecycle; GitHub commit on approval |
| `backend.py` | `SubprocessBackend` (local) vs `ClusterBackend` (srun) |
| `runner.py` | dispatch by canonical type + `tools.source` |
| `handlers/custom_handler.py` | wires an uploaded handler into `HandlerExecutor` + serves it |
| `validator.py` | AST-only upload check (advisory) |
| `port_pool.py` | port allocation |
| `models.py` | `RegisterRequest`, `CustomConfig`, `CredentialRequirement`, `AgentResponse` |
| `card_credentials.py` / `credential_catalog.py` | credential declaration on the card |
| `static/index.html` | registration web UI |

See [BUILDING_AGENTS.md](BUILDING_AGENTS.md) for the agent author's perspective.
