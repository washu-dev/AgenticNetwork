# registration_service

FastAPI service that lets researchers register their own agents into the AgenticNetwork without writing code. It manages agent subprocesses, persists state in PostgreSQL, and publishes agents to ADS so the planner can discover them.

Runs on port **8091**. Accessible through the planner UI at `/register/` (proxied by the planner's FastAPI server).

## Handler types

There are **three**, split on the one question that changes the runtime — *who runs
the loop*:

| Type | Who runs the loop | What it does |
|---|---|---|
| `llm` | our frozen engine (`agent_skeleton.llm_loop`) | An LLM tool loop. `config.tools` selects where the capabilities come from — see the table below. Chat Completions via `OPENAI_BASE_URL`. |
| `proxy` | nobody | Forwards each incoming A2A task to an upstream HTTP service the researcher already runs. No LLM. |
| `custom` | your uploaded code | Runs a researcher-uploaded `agent_skeleton.AgentHandler` subclass, which owns the whole flow. |

### `llm` tool sources (`config.tools.source`)

| Source | Capabilities from | Ships code? |
|---|---|---|
| `none` | nothing — a single LLM turn from the prompt | no |
| `presets` | named tools in the built-in catalog (`agent_skeleton.system_tools.preset_tools`) | no |
| `upload` | author-uploaded tool bodies + schemas (`TOOL_SCHEMAS` + `TOOL_REGISTRY`, or a `build_tools(config)` factory) | **yes** |
| `http` | one typed tool per operation of an external API; a routing prompt is generated, so `system_prompt` is optional | no |
| `raw` | caller-supplied schemas with no bodies — nothing can dispatch them, so the agent runs prompt-only and logs a warning | no |

> **Approval and image builds hang off the CONFIG, not the type name.**
> `models.carries_upload(config)` is the single predicate: it is true for `custom`
> and for `llm` + `tools.source == "upload"`. Those agents are created
> **not-approved**, must have their code uploaded (`POST /agents/{id}/handler`), and
> must be approved by an admin before they spawn; on the cluster and Cloud Run they
> go through the GitHub→Docker Hub per-agent image build (see below).
> `models.upload_spec(config)` returns the shared build parameters, so one
> `generate_dockerfile` serves both.

> **Backend support:** agents that ship no code spawn only via the local
> `SubprocessBackend`. The cluster `ClusterBackend` supports **uploaded-code agents
> only** — registering a prompt-only `llm` or a `proxy` while `SPAWN_BACKEND=cluster`
> is rejected (there is no cluster spawn path, and publishing one would create a
> ghost-live row the planner would dispatch to). The Cloud Run `CloudRunBackend`
> (`SPAWN_BACKEND=cloudrun`) likewise deploys uploaded-code agents only.

### Legacy type names (still accepted)

This taxonomy replaced a five-way split in 2026-07. `tool_loop` and `endpoint_ops`
were never different *runtimes* from `llm` — their handlers (now
`handlers/llm_upload_handler.py` and `handlers/llm_http_handler.py`) import
`llm_handler`'s executor and card builder verbatim; only the `AgentSpec` differed. So
"where do the tools come from" became a field.

| Legacy name | Now |
|---|---|
| `api_proxy` | `proxy` |
| `tool_loop` | `llm` with `tools.source = "upload"` |
| `endpoint_ops` | `llm` with `tools.source = "http"` |

The old names **and their old flat config shapes** are still accepted on the wire and
still parse (`models.parse_config`). That is load-bearing, not politeness: rows
already in Postgres carry the old `handler_type` *and* the old config JSON, and the
read path re-parses them on every spawn. Old rows are normalized **on read**, never
rewritten — so the `agents_handler_type_check` constraint permits both sets, and a
registration submitted under a legacy name is *persisted* under the canonical one.

## Key files

| File | Purpose |
|---|---|
| `server.py` | FastAPI app — REST endpoints for agent CRUD + the web UI at `/` |
| `models.py` | Pydantic models: `RegisterRequest`, `LLMConfig` (+ the `ToolSource` union), `ProxyConfig`, `CustomConfig`, `AgentResponse`; the handler-type aliases and `carries_upload`/`upload_spec` |
| `db.py` | asyncpg connection pool, schema migrations, startup cleanup of stale agents |
| `port_pool.py` | Allocates ports 19200–19299 to spawned agents; `SELECT FOR UPDATE SKIP LOCKED` for concurrency safety |
| `supervisor.py` | Spawns/publishes agents, waits for health, runs a 30s health-check loop, restarts failed agents with backoff; drives the cluster and Cloud Run custom-agent build pipeline |
| `backend.py` | `SubprocessBackend` (local dev), `ClusterBackend` (cluster `srun` steps) and `CloudRunBackend` (Cloud Run services) behind one `ContainerBackend` interface; `make_backend()` picks by `SPAWN_BACKEND` |
| `runner.py` | Entry point for local subprocess agents; reads `AGENT_CONFIG_JSON` and dispatches on the canonical type + `tools.source` → the right handler |
| `builder.py` | Generates the per-agent Docker build context (`Dockerfile` + `{slug}.card.json`) committed to the agent-directory repo for cluster custom agents |
| `handlers/llm_handler.py` | A2A server for the `llm` loop with the `none`, `presets` or `raw` tool source (`raw` runs prompt-only); its executor + card builder are reused verbatim by the two handlers below |
| `handlers/llm_upload_handler.py` | A2A server for an `llm` agent with uploaded tools (`tools.source = "upload"`; legacy name `tool_loop`), local subprocess path |
| `handlers/llm_http_handler.py` | A2A server for an `llm` agent with one typed tool per API operation (`tools.source = "http"`; legacy name `endpoint_ops`) |
| `handlers/proxy_handler.py` | Minimal A2A server that forwards tasks to an upstream URL (the `proxy` type; legacy name `api_proxy`) |
| `handlers/custom_handler.py` | Loads and runs an uploaded `AgentHandler` subclass (local subprocess path) |
| `credential_catalog.py` / `card_credentials.py` | Canonical credential-type catalog + the card credentials extension for declared per-user creds (P4) |
| `validator.py` | Validates uploaded custom-agent handler code/archives before spawn |
| `archive.py` | Safe extraction + inspection of uploaded handler archives |
| `lifespan.py` | FastAPI lifespan: `init_db` → recover live agents → start health/build loops |
| `oasf_skills.py` | Flat OASF skill taxonomy (~90 skills) served at `GET /oasf-skills` |
| `fake_db.py` / `fake_supervisor.py` | In-memory drop-ins used by `FAKE_DB=1` dev mode + tests (`flags.py` parses `FAKE_DB` / `FAKE_SUPERVISOR`) |
| `static/index.html` | Self-contained web UI — registration wizard + live agent table |

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Liveness check |
| `GET` | `/ready` | Readiness: the DB and the directory (`dirctl`); 503 if either is in error |
| `GET` | `/oasf-skills` | Full OASF skill list |
| `GET` | `/credential-catalog` | Known credential types a custom agent may declare |
| `POST` | `/agents` | Register and start a new agent |
| `GET` | `/agents` | List all agents with status |
| `GET` | `/agents/{id}` | Single agent detail |
| `DELETE` | `/agents/{id}` | Stop, unpublish, and remove an agent |
| `POST` | `/agents/{id}/restart` | Manually restart an agent |
| `POST`/`GET` | `/agents/{id}/handler` | Upload / fetch a custom agent's handler code |
| `GET` | `/agents/{id}/handler/download` | Download an uploaded handler archive |
| `POST` | `/agents/{id}/approve` \| `/reject` | Admin approval gate for custom agents |
| `GET`/`POST` | `/internal/{spawn-queue,spawned,stop-queue}` | Host-side cluster spawn-loop coordination (not user-facing). No access check in code: keep port 8091 off public networks. The planner's `/register` proxy refuses these paths (see SECURITY.md) |

## Local development (no postgres, no ADS)

The registration service depends on postgres and ADS at runtime. `FAKE_DB=1`
replaces both with in-memory stubs so you can work on the UI and API without
standing up any external services.

### Run the web UI locally

```bash
cd /path/to/AgenticNetwork

# One-time venv setup
python3 -m venv registration_service/.venv
registration_service/.venv/bin/pip install -r registration_service/requirements.txt

# Start with fake DB (no postgres, no ADS, no subprocesses)
FAKE_DB=1 registration_service/.venv/bin/python -m registration_service.server
```

Open **http://localhost:8091/** for the registration wizard.

In fake mode:
- Registered agents are stored in memory and lost on restart (expected).
- `spawn_and_publish` is a no-op: agents go straight to `status: live` without
  starting a subprocess or contacting ADS.
- The port pool (19200–19299) is managed in memory; port reuse on delete works
  as normal.
- All REST endpoints behave identically to production — only the persistence
  and subprocess layers are stubbed.
- `FAKE_DB=1 FAKE_SUPERVISOR=0` keeps the in-memory DB but spawns and publishes
  real subprocess agents (what `scripts/stack up` runs; see docs/DEVSTACK.md).

### Run the tests

The pytest suite drives the full FastAPI app in `FAKE_DB=1` mode and asserts
on real HTTP responses. No postgres, no ADS, no API keys required.

```bash
# From the repo root (conftest sets FAKE_DB=1)
registration_service/.venv/bin/python -m pytest registration_service/tests -q -p no:cacheprovider
```

Tests cover: register (every type + tool source, canonical and legacy names — see
`tests/test_handler_taxonomy.py`), list, get, delete, restart, port
reuse after delete, validation errors, and skills round-trip.

### How this relates to the planner's `--fake-ads`

The planner has an analogous flag: `--fake-ads` runs its in-process ADS stub so
you can test planner logic without `dirctl`. `FAKE_DB=1` fills the same gap for
the registration service. The two modes are independent — use whichever matches
the component you are working on.

| Mode | What it stubs | When to use |
|---|---|---|
| `planner --fake-ads` | ADS/dirctl directory | Working on planner logic or LLM tool loop |
| `FAKE_DB=1` | postgres + ADS + subprocesses | Working on registration UI, API, or handlers |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `REGISTRATION_DB_DSN` | — | Full asyncpg DSN. If unset, constructed from `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` |
| `PGHOST` | `localhost` | Postgres host |
| `PGPORT` | `5432` | Postgres port |
| `PGUSER` | `postgres` | Postgres user |
| `PGDATABASE` | `registration` | Database name |
| `PGPASSWORD` | — | Postgres password (used when constructing the DSN from parts) |
| `ADS_SERVER_ADDR` | — | ADS dirctl address, e.g. `localhost:8888` (consumed via `agentnet.directory.publisher`) |
| `OPENAI_BASE_URL` | — | vLLM endpoint for spawned LLM agents |
| `OPENAI_API_KEY` | — | API key (any non-empty value works with vLLM) |
| `REGISTRATION_SERVICE_PORT` | `8091` | Port the FastAPI server listens on |
| `REGISTRATION_SERVICE_HOST` | `0.0.0.0` | Address the FastAPI server binds (`python -m registration_service.server`) |
| `ADMIN_EMAILS` | — | Comma-separated admin allowlist (matched against SSO identity); fail-closed |
| `SPAWN_BACKEND` | `subprocess` | `subprocess` / `cluster` / `cloudrun`; `cluster` and `cloudrun` add the GitHub→Docker Hub custom-agent build pipeline |
| `GITHUB_TOKEN` / `GITHUB_REPO` | — | PAT + repo (`washu-dev/agent-directory`) for the cluster / Cloud Run custom-agent build |
| `HANDLER_FILES_DIR` | `./uploaded_handlers` | Where uploaded custom-agent handler files are stored |
| `CUSTOM_AGENT_PORT_START` / `CUSTOM_AGENT_PORT_END` | `19200` / `19299` | Port range for spawned agents |
| `AGENT_BIND_HOST` | `0.0.0.0` | Address subprocess agents bind (the dev stack sets `127.0.0.1`) |
| `AGENT_LOG_DIR` | `logs/agents` | Per-agent log files of subprocess agents |
| `AGENT_SPAWN_HEALTH_TIMEOUT_SECONDS` | `30` | How long a subprocess agent may take to answer its first health check |
| `MAX_ARCHIVE_FILES` / `MAX_ARCHIVE_UNCOMPRESSED` | `2000` / 25 MB | Limits for uploaded handler archives |
| `FAKE_DB` / `FAKE_SUPERVISOR` | off / = `FAKE_DB` | In-memory DB; no-op supervisor (see Local development) |
| GCP (`cloudrun`) | — | `GCP_PROJECT`, `GCP_REGION`, `AGENT_RUNTIME_SA`, `PLANNER_INVOKER_SA`, `VPC_CONNECTOR`, `CLOUDRUN_FORWARD_ENV`; see docs/deployment/gcp/ |

## Deployment (ENGR cluster)

The registration service runs as an `srun --overlap` step in `deployment/submit.sbatch`. PostgreSQL runs in its **own** dedicated container (`Dockerfile.postgres` / `entrypoint-postgres.sh`), which owns the `initdb`/startup and the `/engrfs/project/di2/postgres_data` data volume; the registration container is a pure DB **client**.

The entrypoint (`deployment/entrypoints/entrypoint-registration.sh`) handles:
- Waiting for the external PostgreSQL (`localhost:5432`) to accept connections
- Waiting for ADS before starting the FastAPI server

Custom-agent spawning on the cluster uses `SPAWN_BACKEND=cluster`: the container can't call `srun`, so a host-side loop (`deployment/spawn_agent_loop.py`) polls the `/internal/*` endpoints and launches approved agents as `srun --overlap` steps.

The planner proxies `/register/{path}` to `http://localhost:8091` so the UI is reachable through the planner's public port without exposing 8091 externally.

## Deployment (GCP Cloud Run)

With `SPAWN_BACKEND=cloudrun` the service deploys each approved uploaded-code agent as its own Cloud Run service. See [docs/deployment/gcp/README.md](../docs/deployment/gcp/README.md).
