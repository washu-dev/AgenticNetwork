# Multi-Spider Agent Command Center

All intelligence centralized: planner + pi agent brains + detection.
Pi robots are thin clients (video stream + command execution only).

## Architecture

```
Robot (Thin Client)                Command Center (Central Intelligence)
┌──────────────────┐              ┌──────────────────────────────────────┐
│ remote_stream.py │──video────►  │ detection/engine.py (YOLO/InsightFace)│
│   (port 9000)    │              │         │ in-process                  │
│                  │              │ pi_agents/base_agent.py (1 per robot) │
│ thin_client.py   │◄──NATS────  │   ├─ LLM decisions                   │
│   (port 9010)    │   command    │   ├─ patrol logic                    │
│   /execute       │              │   └─ mission management              │
│   /ultrasonic    │              │         │                             │
│   /status        │              │ planner/graph.py (task classification)│
│   /speak         │              │ infra/robot_registry.py              │
│                  │              │ gateway.py (unified API + frontend)  │
│ registers to ────┼──ADS─────►  │                                      │
│   ADS on startup │              └──────────────────────────────────────┘
└──────────────────┘

      ADS (Docker)                NATS (Docker)
    ┌─────────────┐             ┌──────────────┐
    │ gRPC :8888  │             │ Client :4222 │
    │ Health :8889│             │ Monitor :8222│
    └─────────────┘             └──────────────┘
```

**Flow**: User command → Planner classifies task → Detection engine grabs video frames & runs YOLO → Passes detections in-process to CentralPiAgent → LLM decides action → NATS sends command to robot → Robot executes on hardware

## Prerequisites

- Python 3.10+
- Docker & Docker Compose
- OpenAI API Key (or Anthropic API Key)

## Project Structure

```
Multi_Spider_Agent_Command_Center/
├── gateway.py                  # FastAPI app: REST API + frontend serving
├── main.py                     # Entry point (uvicorn server)
├── config.py                   # Configuration (env vars)
├── .env                        # Environment variables (API keys, ports)
├── start.sh                    # Start Command Center
├── stop.sh                     # Stop Command Center
├── requirements.txt            # Python dependencies
│
├── frontend/                   # Frontend UI
│   └── index.html
│
├── planner/                    # LangGraph mission planner
│   ├── graph.py                # Task classification & agent dispatch
│   └── llm_config.py           # LLM config (LiteLLM)
│
├── pi_agents/                  # Centralized pi agent brains
│   ├── base_agent.py           # CentralPiAgent + MissionGraph + PiAgentManager
│   └── brain.py                # LLM client (observation → action decisions)
│
├── detection/                  # Computer vision
│   ├── engine.py               # Grabs MJPEG frames, runs detection loop
│   ├── yolo_detector.py        # YOLO object detection
│   ├── face_registry.py        # Face recognition (InsightFace)
│   └── memory.py               # Detection history (SQLite)
│
├── infra/                      # Infrastructure & transport
│   ├── transport.py            # NATS/SLIM transport abstraction
│   ├── a2a_client.py           # A2A protocol client (JSON-RPC over transport)
│   ├── robot_client.py         # High-level robot command client
│   ├── robot_registry.py       # Robot state tracking (online, standing, busy)
│   ├── discovery.py            # ADS agent discovery
│   └── shared.py               # Transport singleton
│
├── scripts/                    # Infrastructure scripts
│   ├── start_ads.sh            # Start ADS (Docker)
│   ├── stop_ads.sh             # Stop ADS
│   ├── start_nats.sh           # Start NATS (Docker)
│   └── stop_nats.sh            # Stop NATS
│
├── docker-compose-ads.yaml     # ADS services (dir-api-server + zot + oasf)
└── docker-compose-nats.yaml    # NATS server (JetStream)
```

## Setup

### 1. Configure environment

Copy and edit `.env`:

```bash
cd Multi_Spider_Agent_Command_Center

# Edit .env with your API keys
vim .env
```

Key variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MODEL` | `openai/gpt-4o-mini` | LLM model for planner and pi agent brain |
| `OPENAI_API_KEY` | — | OpenAI API key (required) |
| `GATEWAY_PORT` | `8080` | Command Center HTTP port |
| `PI_ALPHA_HOST` | — | Pi robot IP address |

### 2. Install Python dependencies

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Start infrastructure services

ADS and NATS are both Docker services. Start them **before** the Command Center:

```bash
# Start ADS (Agent Directory Service) — robot discovery
./scripts/start_ads.sh

# Start NATS — message transport between Command Center and robots
./scripts/start_nats.sh
```

Verify services are running:

```bash
# ADS health check
curl -s http://localhost:8889/healthz/ready

# NATS monitoring
curl -s http://localhost:8222/varz | head -5
```

### 4. Start Command Center

```bash
./start.sh
```

The server starts on `http://localhost:8080`.

Open the browser to see the frontend UI.

## Services Overview

| Service | Port | Protocol | Description |
|---------|------|----------|-------------|
| Command Center | 8080 | HTTP | Main API + frontend UI |
| ADS (gRPC) | 8888 | gRPC | Agent Directory Service — robot discovery |
| ADS (health) | 8889 | HTTP | ADS health check endpoint |
| NATS (client) | 4222 | TCP | Message transport — commands to robots |
| NATS (monitor) | 8222 | HTTP | NATS monitoring dashboard |
| Zot (OCI) | 5555 | HTTP | OCI registry for ADS storage |
| OASF | 31234 | HTTP | A2A card → OASF translation |

## Stopping

```bash
# Stop Command Center
./stop.sh

# Stop infrastructure
./scripts/stop_nats.sh
./scripts/stop_ads.sh
```

## NATS Transport

Commands are sent to robots via NATS topics:

| Topic | Usage |
|-------|-------|
| `agents.direct.{robot_id}` | Direct command to a specific robot |
| `agents.broadcast` | Broadcast to all robots |

If NATS is unavailable, the system automatically falls back to direct HTTP.

Configure transport in `.env` or environment:

```bash
DEFAULT_MESSAGE_TRANSPORT=NATS            # NATS or SLIM
TRANSPORT_SERVER_ENDPOINT=nats://localhost:4222
```

## Pi Robot (Thin Client) Setup

Each Pi robot needs to:

1. Run `remote_stream.py` on port 9000 (MJPEG video stream)
2. Run `thin_client.py` on port 9010 (command executor)
3. Register to ADS on startup
4. Connect to NATS for receiving commands

Required config on Pi side:

```bash
TRANSPORT_SERVER_ENDPOINT=nats://<command-center-ip>:4222
CENTRAL_HOST=<command-center-ip>
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Frontend UI |
| `GET` | `/health` | Health check |
| `GET` | `/api/robots` | List all robots and status |
| `POST` | `/api/robots/register` | Register a robot |
| `POST` | `/api/robots/{id}/heartbeat` | Robot heartbeat |
| `POST` | `/api/tasks` | Submit a task (natural language) |
| `GET` | `/api/mission/active` | Get active missions |
| `POST` | `/api/discovery/refresh` | Re-discover robots via ADS |
