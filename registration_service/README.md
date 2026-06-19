# registration_service

FastAPI service that lets researchers register their own agents into the AgenticNetwork without writing code. It manages agent subprocesses, persists state in PostgreSQL, and publishes agents to ADS so the planner can discover them.

Runs on port **8091**. Accessible through the planner UI at `/register/` (proxied by the planner's FastAPI server).

## Handler types

| Type | What it does |
|---|---|
| `llm` | Spawns a subprocess running a generic LLM agent with a researcher-provided system prompt. Uses the Chat Completions API via `OPENAI_BASE_URL`. |
| `api_proxy` | Forwards incoming A2A tasks to an existing upstream HTTP service the researcher already runs. |

## Key files

| File | Purpose |
|---|---|
| `server.py` | FastAPI app — REST endpoints for agent CRUD + static file serving |
| `models.py` | Pydantic models: `RegisterRequest`, `LLMConfig`, `APIProxyConfig`, `AgentResponse` |
| `db.py` | asyncpg connection pool, schema migrations, startup cleanup of stale agents |
| `port_pool.py` | Allocates ports 19200–19299 to spawned agents; `SELECT FOR UPDATE SKIP LOCKED` for concurrency safety |
| `supervisor.py` | Spawns subprocesses, waits for health, publishes to ADS, runs a 30s health-check loop, restarts failed agents with exponential backoff |
| `backend.py` | `SubprocessBackend` — asyncio subprocess management, health polling via `GET /health` |
| `runner.py` | Entry point for spawned agent subprocesses; reads `AGENT_CONFIG_JSON` from env and starts the right handler |
| `handlers/llm_handler.py` | Minimal A2A server backed by a single-turn Chat Completions call |
| `handlers/api_proxy_handler.py` | Minimal A2A server that forwards tasks to an upstream URL |
| `lifespan.py` | FastAPI lifespan: `init_db` → recover live agents → start health loop |
| `oasf_skills.py` | Flat OASF skill taxonomy (~90 skills) served at `GET /oasf-skills` |
| `static/index.html` | Self-contained web UI — registration wizard + live agent table |

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Liveness check |
| `GET` | `/oasf-skills` | Full OASF skill list |
| `POST` | `/agents` | Register and start a new agent |
| `GET` | `/agents` | List all agents with status |
| `GET` | `/agents/{id}` | Single agent detail |
| `DELETE` | `/agents/{id}` | Stop, unpublish, and remove an agent |
| `POST` | `/agents/{id}/restart` | Manually restart an agent |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `REGISTRATION_DB_DSN` | — | Full asyncpg DSN. If unset, constructed from `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` |
| `PGHOST` | `localhost` | Postgres host |
| `PGPORT` | `5432` | Postgres port |
| `PGUSER` | `postgres` | Postgres user |
| `PGDATABASE` | `registration` | Database name |
| `ADS_SERVER_ADDR` | — | ADS dirctl address, e.g. `localhost:8888` |
| `OPENAI_BASE_URL` | — | vLLM endpoint for spawned LLM agents |
| `OPENAI_API_KEY` | — | API key (any non-empty value works with vLLM) |
| `REGISTRATION_SERVICE_PORT` | `8091` | Port the FastAPI server listens on |

## Deployment (ENGR cluster)

The registration service runs as an `srun --overlap` step in `deployment/submit.sbatch`. PostgreSQL runs inside the same container, with its data directory bind-mounted from `/engrfs/project/di2/postgres_data` so state survives job restarts.

The entrypoint (`deployment/entrypoints/entrypoint-registration.sh`) handles:
- `initdb` on first run, SysV shared memory config (required in HPC containers)
- Starting postgres on a unix socket at `/tmp/postgresql` (avoids `/var/run` issues in enroot)
- Waiting for ADS before starting the FastAPI server

The planner proxies `/register/{path}` to `http://localhost:8091` so the UI is reachable through the planner's public port without exposing 8091 externally.
