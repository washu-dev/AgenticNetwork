# AgenticNetwork

A demo of an **"Internet of Agents"**: independent, LLM-powered services that discover each other through a shared directory and collaborate over a standard agent-to-agent protocol. A user talks to one orchestrator (the **planner**); the planner discovers specialist agents at runtime and delegates tasks to them — with no hardcoded knowledge of which agents exist.

## How it works

```
        You (browser / curl)
              │
              ▼
        ┌───────────┐   "who can send email?"    ┌─────────────┐
        │  PLANNER  │ ─────────────────────────▶ │     ADS     │  agent registry
        │  :8090    │ ◀──── card + endpoint ───── │    :8888    │
        └─────┬─────┘                             └─────────────┘
              │
              │  A2A call (HTTP/JSON-RPC)
              ▼
   ┌──────────┬──────────┬─────────────┬──────────┬──────────┐
   │  email   │  coding  │  disaster   │ geospatial│  intel  │
   │  :9103   │  :9106   │   :9104     │   :9105   │  :9105  │
   └──────────┴──────────┴─────────────┴──────────┴──────────┘
```

Three contracts hold the system together:

| Contract | Role | Implementation |
|---|---|---|
| **Agent Card** (`*.card.json`) | An agent's résumé: name, URL, skills | lives next to each agent's code |
| **ADS** (Agent Directory Service) | The registry: publish your card, search by skill | AGNTCY `dirctl` daemon |
| **A2A** (agent-to-agent protocol) | The call: send a task over HTTP/JSON-RPC, get a result | `a2a-sdk` |

Adding a new agent to ADS makes it available to the planner with **zero planner code changes**.

## Components

| Directory | Role | Port | Full docs |
|---|---|---|---|
| [`planner_agent/`](planner_agent/) | Orchestrator + chat UI | 8090 | [README](planner_agent/README.md) |
| [`agent_directory_service/`](agent_directory_service/) | ADS operator tooling (cluster ops) | — | [README](agent_directory_service/README.md) |
| [`email_agent/`](email_agent/) | Gmail, Calendar, Docs, Sheets | 9103 | [README](email_agent/README.md) |
| [`disaster_response_agent/`](disaster_response_agent/) | Emergency response planning | 9104 | [README](disaster_response_agent/README.md) |
| [`geoSpatial_agent/`](geoSpatial_agent/) | Photo geolocation + incident classification | 9105 | [README](geoSpatial_agent/README.md) |
| [`intelligence_agent/`](intelligence_agent/) | Situational awareness + threat assessment | 9105 | [README](intelligence_agent/README.md) |
| [`coding_agent/`](coding_agent/) | Code writing via mini-swe-agent | 9106 | [README](coding_agent/README.md) |
| [`scout_agent/`](scout_agent/) | OSINT web research | 9104 | [README](scout_agent/README.md) |
| [`deployment/`](deployment/) | ENGR cluster containerized deployment | — | [README](deployment/README.md) |
| [`registration_service/`](registration_service/) | Agent self-registration web UI + manager | — | [README](registration_service/README.md) |

> **Port collisions:** geospatial and intelligence both default to 9105; disaster and scout both default to 9104. Fine on separate machines or containers; reassign ports if running several on one host.

## Deployment

The system runs on the WashU **ENGR cluster** via Slurm + Pyxis/enroot. Each component is a Docker image; `deployment/submit.sbatch` starts them all as parallel containers on one node, with ADS coming up first and agents self-publishing on startup.

See [`deployment/README.md`](deployment/README.md) for the full cluster runbook.

For local development without the cluster, the planner supports a **fake-ADS mode** that runs an in-process directory — useful for testing the planner against specific agent cards without standing up the full stack:

```bash
cd /path/to/AgenticNetwork
python3 -m venv planner_agent/.venv
planner_agent/.venv/bin/python -m pip install -r planner_agent/requirements.txt
cp planner_agent/.env.example planner_agent/.env.local  # add OPENAI_API_KEY
planner_agent/scripts/start.sh --fake-ads \
  --card email_agent/email_agent.card.json \
  --card coding_agent/coding_agent.card.json
```

Open **http://localhost:8090/** for the chat UI.

## Environments

Each component is isolated — do not install one component's requirements into another's environment. They communicate only over HTTP (A2A), so dependency versions don't need to align.

| Environment | Components |
|---|---|
| pip venv | `planner_agent` |
| pip venv (editable) | `scout_agent` |
| conda `agent-directory-service` | `agent_directory_service` |
| conda `email-agent-a2a` (shared) | `email_agent`, `disaster_response_agent`, `geoSpatial_agent` |
| conda `coding-agent-a2a` | `coding_agent` |
| conda `intelligence-agent-a2a` | `intelligence_agent` |

## Agent Cards

Each agent owns its card — a JSON file that describes its name, endpoint URL, and skills. Cards live next to the agent code and are published to ADS on startup:

```
email_agent/email_agent.card.json
coding_agent/coding_agent.card.json
disaster_response_agent/disaster_response_agent.card.json
intelligence_agent/intelligence_agent.card.json
geoSpatial_agent/geospatial_agent.card.json
```

The planner uses ADS to discover agents at runtime. In fake-ADS mode for local dev, pass cards directly with `--card`.

## Adding a new agent

1. Write an Agent Card (`my_agent.card.json`) with a name, URL, and `skills[]` list.
2. Implement an `AgentExecutor` (extend the `a2a-sdk` class).
3. Serve it with `A2AStarletteApplication` + uvicorn.
4. Publish the card to ADS on startup — the planner discovers it automatically.

See [`CLAUDE.md`](CLAUDE.md) §6 for the full step-by-step guide.

## Further reading

- [`CLAUDE.md`](CLAUDE.md) — architecture deep-dive, LLM configuration, vLLM self-hosting, known caveats
- [`AGENTS.md`](AGENTS.md) — working rules and validation commands for contributors
- [`deployment/README.md`](deployment/README.md) — ENGR cluster runbook
