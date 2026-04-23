# Multi-Spider Agent Command Center

All intelligence centralized: planner + pi agent brains + detection.
Pi robots are thin clients (video stream + command execution only).
Robot Agents are **real A2A servers** (a2a-sdk + agntcy-app-sdk) — the
Planner talks to them as standards-compliant A2A clients, transport
pluggable across NATS / HTTP / SLIM.

## Architecture

```
Robot (Thin Client)             Robot Agent (per-robot process)          Command Center
┌──────────────────┐            ┌──────────────────────────────┐        ┌─────────────────────────┐
│ remote_stream.py │──UDP──────►│ detection engine (YOLO/Face) │        │  Planner (LangGraph)     │
│    (port 9000)   │   video    │   pi_agents/base_agent.py    │◄──A2A──┤  planner/a2a_client.py   │
│                  │            │   robot_a2a/executor.py      │ JSON-  │  (A2AClientFactory)      │
│  thin_client.py  │◄──HTTP─────┤   ├── A2AStarletteApplication│  RPC   │                          │
│    (port 9004)   │  /command  │   │   on HTTP :9100+         │ over   │  gateway.py (FastAPI)    │
│  /command        │            │   └── agntcy AppSession on   │ NATS   │    /api/search           │
│  /capabilities   │            │       NATS topic             │  or    │    /api/robots           │
│  /health         │            │       agents.direct.{id}     │  HTTP  │    WebSocket / UI        │
└──────────────────┘            │   reasoning/state pub/sub    │        │  infra/discovery.py      │
                                │   → agents.stream.{id}.*     │        │  infra/robot_registry.py │
                                └──────────────┬───────────────┘        └───────────┬─────────────┘
                                               │                                     │
                                               └──────── NATS pub/sub ───────────────┘
                                                (reasoning/state → gateway UI)

   ADS (Docker)            NATS (Docker)             SLIM (optional)       OASF translation (optional)
  ┌─────────────┐        ┌──────────────┐           ┌──────────────┐      ┌──────────────┐
  │ gRPC :8888  │        │ Client :4222 │           │ :46357       │      │ :31234       │
  │ Health :8889│        │ Monitor :8222│           │              │      │              │
  └─────────────┘        └──────────────┘           └──────────────┘      └──────────────┘
```

## A2A Transport Selection

All three transports are served simultaneously (declared in
`card.additional_interfaces`).  The env var `DEFAULT_MESSAGE_TRANSPORT`
picks the **preferred** one; A2A clients negotiate against the card
automatically, so swapping is transparent.

| Env combo | Preferred | Card fields served |
|-----------|-----------|--------------------|
| `DEFAULT_MESSAGE_TRANSPORT=NATS` (default) | NATS patterns | jsonrpc + natspatterns |
| `DEFAULT_MESSAGE_TRANSPORT=HTTP` | JSONRPC over HTTP | jsonrpc |
| `DEFAULT_MESSAGE_TRANSPORT=SLIM` + `SLIM_SHARED_SECRET` | SLIM patterns | jsonrpc + slimpatterns |

Regardless of A2A transport, reasoning/state streaming (Robot Agent →
Gateway UI) always uses NATS pub/sub on `agents.stream.{robot_id}.*`.

## Prerequisites

- **Python ≥ 3.12** (required — `agntcy-app-sdk` does not support 3.11)
- Docker Desktop (for ADS + NATS + optional OASF translation service)
- API Key (OpenAI / Anthropic / etc.)

## Setup

### Option 1 — conda env (recommended, 3.12)

```bash
conda create -n mas_spider python=3.12 -y
conda activate mas_spider
cd Command_Center
pip install -r requirements.txt
```

Then tell the start scripts where to find it:

```bash
export PYTHON_BIN=/path/to/miniconda3/envs/mas_spider/bin/python
```

(The start scripts also auto-detect `mas_spider` at the default miniconda
path, so you can skip `PYTHON_BIN` on that setup.)

### Option 2 — venv (only if your system Python is ≥ 3.12)

```bash
cd Command_Center
python3.12 -m venv venv
source venv/bin/activate        # Linux/macOS
# or: .\venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

### Configure credentials

Edit `Command_Center/.env` with your API key. `.env` is `.gitignore`d —
it will NOT be committed.

## Startup Order

> Pi Robots must be powered on and on the same network before step 4.

| Step | Linux / macOS | Windows (`windows/` folder) |
|------|---------------|-----------------------------|
| 1. Start Docker Desktop | — | — |
| 2. Start ADS | `./scripts/start_ads.sh` | `.\start_ads.bat` |
| 3. Start NATS | `./scripts/start_nats.sh` | `.\start_nats.bat` |
| 4. Start Robot Agents | `./start_robots.sh` | `.\start_robots.bat` |
| 5. Start Command Center | `./start.sh` | `.\start.bat` |

Windows one-click: `.\start_all.bat`

## Shutdown

| Linux / macOS | Windows |
|---------------|---------|
| `./stop.sh` | `.\stop.bat` |
| `./scripts/stop_nats.sh` | `.\stop_nats.bat` |
| `./scripts/stop_ads.sh` | `.\stop_ads.bat` |

Windows one-click: `.\stop_all.bat`

## Configuration

Edit `robots.json` to set Pi robot IPs:

```json
[
    {"robot_id": "crawler-alpha-patrol-002", "pi_host": "192.168.1.194", "zone": "A", "robot_type": "spider"}
]
```

Key `.env` / env variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MODEL` | `openai/gpt-4o-mini` | LLM provider/model |
| `OPENAI_API_KEY` | — | API key (or `ANTHROPIC_API_KEY`, etc.) |
| `GATEWAY_PORT` | `8081` | Command Center HTTP port |
| `DISCOVERY_MODE` | `static` | `ads` or `static` |
| `DEFAULT_MESSAGE_TRANSPORT` | `NATS` | A2A preferred: `NATS` / `HTTP` / `SLIM` |
| `A2A_ENABLE_HTTP` | `true` | Serve HTTP JSONRPC A2A endpoint |
| `A2A_ENABLE_NATS` | auto | Serve NATS patterns A2A endpoint |
| `A2A_ENABLE_SLIM` | auto | Serve SLIM patterns A2A endpoint |
| `NATS_ENDPOINT` | `nats://localhost:4222` | NATS broker |
| `SLIM_ENDPOINT` | `http://localhost:46357` | SLIM node |
| `SLIM_SHARED_SECRET` | — | Required if SLIM enabled |
| `A2A_HTTP_PORT_BASE` | `9100` | Per-robot HTTP port base (hashed by robot_id) |
| `PYTHON_BIN` | auto | Override Python used by start scripts |

## Services

| Service | Port | Description |
|---------|------|-------------|
| Command Center | 8081 | API + frontend UI |
| Robot Agent A2A HTTP | 9100+ (per robot) | A2A JSONRPC endpoint per robot (hashed by robot_id) |
| ADS (gRPC) | 8888 | Agent discovery |
| NATS | 4222 | A2A transport (patterns) + reasoning/state pub/sub |
| NATS Monitor | 8222 | NATS dashboard |
| SLIM (optional) | 46357 | Alternative A2A transport |
| OASF translation (optional) | 31234 | Canonical A2A→OASF record conversion |

## Notes

- Robot Agents auto-reconnect to Pi when Pi comes back online (health monitor polls every 15s).
- `robots.json` IPs may change across environments — update before each run.
- Planner dispatches tasks over **A2A JSON-RPC** to each Robot Agent; the
  transport under A2A can be NATS / HTTP / SLIM, picked per `DEFAULT_MESSAGE_TRANSPORT`.
- Reasoning / state streaming to the UI always uses NATS pub/sub
  (`agents.stream.{robot_id}.reasoning` and `.state`).
- `Command_Center/mock/` is a dev-only replay harness (mp4 → UDP → Robot Agent)
  and is `.gitignore`d.
