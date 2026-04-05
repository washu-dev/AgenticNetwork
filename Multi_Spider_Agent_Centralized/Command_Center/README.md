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

## Prerequisites

- Python 3.10+
- Docker Desktop
- API Key (OpenAI / Anthropic / etc.)

## Setup

```bash
# 1. Create venv & install dependencies
cd command_center
python -m venv venv

# Linux/macOS
source venv/bin/activate

# Windows
.\venv\Scripts\activate

pip install -r requirements.txt

# 2. Configure .env with your API key
#    Edit command_center/.env
```

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

Key `.env` variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MODEL` | `openai/gpt-4o-mini` | LLM provider/model |
| `OPENAI_API_KEY` | — | API key (or `ANTHROPIC_API_KEY`, etc.) |
| `GATEWAY_PORT` | `8080` | Command Center HTTP port |
| `DISCOVERY_MODE` | `static` | `ads` or `static` |

## Services

| Service | Port | Description |
|---------|------|-------------|
| Command Center | 8080 | API + frontend UI |
| ADS (gRPC) | 8888 | Agent discovery |
| NATS | 4222 | Message transport |
| NATS Monitor | 8222 | NATS dashboard |

## Notes

- Robot Agents do **not** auto-reconnect to Pi. If Pi comes online later, restart `start_robots`.
- `robots.json` IPs may change across environments — update before each run.
- Planner communicates with Robot Agents via NATS (by `robot_id`), not by IP.
