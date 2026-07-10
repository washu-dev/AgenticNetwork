# registration_service

FastAPI service that lets researchers register their own agents into the AgenticNetwork without writing code. It manages agent subprocesses, persists state in PostgreSQL, and publishes agents to ADS so the planner can discover them.

Runs on port **8091**. Accessible through the planner UI at `/register/` (proxied by the planner's FastAPI server).

## Handler types

| Type | What it does |
|---|---|
| `llm` | Spawns a subprocess running a generic LLM agent with a researcher-provided system prompt. Uses the Chat Completions API via `OPENAI_BASE_URL`. |
| `api_proxy` | Forwards incoming A2A tasks to an existing upstream HTTP service the researcher already runs. |
| `custom` | Runs researcher-uploaded handler code (a subclass of `agent_skeleton.AgentHandler`). Requires admin approval; on the cluster it goes through a GitHub→Docker Hub per-agent image build (see below). |

> **Backend support:** `llm`/`api_proxy` currently spawn only via the local
> `SubprocessBackend`. The cluster `ClusterBackend` supports **`custom` only** —
> registering an `llm`/`api_proxy` agent while `SPAWN_BACKEND=cluster` is rejected
> (it has no cluster spawn path).

## Key files

| File | Purpose |
|---|---|
| `server.py` | FastAPI app — REST endpoints for agent CRUD + static file serving |
| `models.py` | Pydantic models: `RegisterRequest`, `LLMConfig`, `APIProxyConfig`, `AgentResponse` |
| `db.py` | asyncpg connection pool, schema migrations, startup cleanup of stale agents |
| `port_pool.py` | Allocates ports 19200–19299 to spawned agents; `SELECT FOR UPDATE SKIP LOCKED` for concurrency safety |
| `supervisor.py` | Spawns/publishes agents, waits for health, runs a 30s health-check loop, restarts failed agents with backoff; drives the cluster custom-agent build pipeline |
| `backend.py` | `SubprocessBackend` (local dev) + `ClusterBackend` (cluster `srun` steps) behind one `ContainerBackend` interface; `make_backend()` picks by `SPAWN_BACKEND` |
| `runner.py` | Entry point for local subprocess agents; reads `AGENT_CONFIG_JSON` and dispatches `handler_type` → the right handler |
| `builder.py` | Generates the per-agent Docker build context (`Dockerfile` + `{slug}.card.json`) committed to the agent-directory repo for cluster custom agents |
| `handlers/llm_handler.py` | Minimal A2A server backed by a single-turn Chat Completions call |
| `handlers/api_proxy_handler.py` | Minimal A2A server that forwards tasks to an upstream URL |
| `handlers/custom_handler.py` | Loads and runs an uploaded `AgentHandler` subclass (local subprocess path) |
| `credential_catalog.py` / `card_credentials.py` | Canonical credential-type catalog + the card credentials extension for declared per-user creds (P4) |
| `validator.py` | Validates uploaded custom-agent handler code/archives before spawn |
| `archive.py` | Safe extraction + inspection of uploaded handler archives |
| `lifespan.py` | FastAPI lifespan: `init_db` → recover live agents → start health/build loops |
| `oasf_skills.py` | Flat OASF skill taxonomy (~90 skills) served at `GET /oasf-skills` |
| `fake_db.py` / `fake_supervisor.py` | In-memory drop-ins used by `FAKE_DB=1` dev mode + tests |
| `static/index.html` | Self-contained web UI — registration wizard + live agent table |

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Liveness check |
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
| `GET`/`POST` | `/internal/{spawn-queue,spawned,stop-queue}` | Host-side cluster spawn-loop coordination (not user-facing) |

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
FAKE_DB=1 PYTHONPATH=. registration_service/.venv/bin/python -m registration_service.server
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

### Run the tests

The pytest suite drives the full FastAPI app in `FAKE_DB=1` mode and asserts
on real HTTP responses. No postgres, no ADS, no API keys required.

```bash
# From the repo root (PYTHONPATH must include the repo root so ads_utils resolves)
FAKE_DB=1 PYTHONPATH=. registration_service/.venv/bin/python -m pytest \
  registration_service/tests -v
```

Tests cover: register (LLM + api_proxy), list, get, delete, restart, port
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
| `ADS_SERVER_ADDR` | — | ADS dirctl address, e.g. `localhost:8888` (consumed via `ads_utils.publisher`) |
| `OPENAI_BASE_URL` | — | vLLM endpoint for spawned LLM agents |
| `OPENAI_API_KEY` | — | API key (any non-empty value works with vLLM) |
| `REGISTRATION_SERVICE_PORT` | `8091` | Port the FastAPI server listens on |
| `ADMIN_EMAILS` | — | Comma-separated admin allowlist (matched against SSO identity); fail-closed |
| `SPAWN_BACKEND` | `subprocess` | `cluster` activates `ClusterBackend` + the GitHub→Docker Hub custom-agent build pipeline |
| `GITHUB_TOKEN` / `GITHUB_REPO` | — | PAT + repo (`washu-dev/agent-directory`) for the cluster custom-agent build |
| `HANDLER_FILES_DIR` | — | Where uploaded custom-agent handler files are stored |

## Deployment (ENGR cluster)

The registration service runs as an `srun --overlap` step in `deployment/submit.sbatch`. PostgreSQL runs in its **own** dedicated container (`Dockerfile.postgres` / `entrypoint-postgres.sh`), which owns the `initdb`/startup and the `/engrfs/project/di2/postgres_data` data volume; the registration container is a pure DB **client**.

The entrypoint (`deployment/entrypoints/entrypoint-registration.sh`) handles:
- Waiting for the external PostgreSQL (`localhost:5432`) to accept connections
- Waiting for ADS before starting the FastAPI server

Custom-agent spawning on the cluster uses `SPAWN_BACKEND=cluster`: the container can't call `srun`, so a host-side loop (`deployment/spawn_agent_loop.py`) polls the `/internal/*` endpoints and launches approved agents as `srun --overlap` steps.

The planner proxies `/register/{path}` to `http://localhost:8091` so the UI is reachable through the planner's public port without exposing 8091 externally.
