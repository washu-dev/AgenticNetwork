# AgenticNetwork

A demo of an **"Internet of Agents"**: independent, LLM-powered services that discover each other through a shared directory and collaborate over a standard agent-to-agent protocol. A user talks to one orchestrator (the **planner**); the planner discovers specialist agents at runtime and delegates tasks to them — with no hardcoded knowledge of which agents exist.

## How it works

```
        You (browser / curl)
              │
              ▼
        ┌───────────┐   "who can send email?"     ┌─────────────┐
        │  PLANNER  │ ─────────────────────────▶  │     ADS     │  agent registry
        │  :8090    │ ◀──── card + endpoint ───── │    :8888    │
        └─────┬─────┘                             └─────────────┘
              │
              │  A2A call (HTTP/JSON-RPC)
              ▼
   ┌──────────┬──────────┬─────────────┬───────────┬──────────┐
   │  email   │  coding  │  disaster   │ geospatial│  intel   │
   │  :9103   │  :9106   │   :9104     │   :9105   │  :9105   │
   └──────────┴──────────┴─────────────┴───────────┴──────────┘
```

Three contracts hold the system together:

| Contract | Role | Implementation |
|---|---|---|
| **Agent Card** (`*.card.json`) | An agent's résumé: name, URL, skills | lives next to each agent's code |
| **ADS** (Agent Directory Service) | The registry: publish your card, search by skill | AGNTCY `dirctl` daemon |
| **A2A** (agent-to-agent protocol) | The call: send a task over HTTP/JSON-RPC, get a result | `a2a-sdk` |

Adding a new agent to ADS makes it available to the planner with **zero planner code changes**.

## Components

**This repo — infrastructure:**

| Directory | Role | Port | Full docs |
|---|---|---|---|
| [`planner_agent/`](planner_agent/) | Orchestrator + chat UI | 8090 | [README](planner_agent/README.md) |
| [`agent_directory_service/`](agent_directory_service/) | ADS operator tooling (cluster ops) | — | [README](agent_directory_service/README.md) |
| [`registration_service/`](registration_service/) | Agent self-registration web UI + manager | — | [README](registration_service/README.md) |
| [`agent_skeleton/`](agent_skeleton/) | Template for building new agents | — | [README](agent_skeleton/CLAUDE.md) |
| [`deployment/`](deployment/) | ENGR cluster containerized deployment | — | [README](deployment/README.md) |

**[`agent-directory`](https://github.com/washu-dev/agent-directory) repo — specialist agents:**

| Agent | Role | Port |
|---|---|---|
| `email_agent` | Gmail, Calendar, Docs, Sheets | 9103 |
| `disaster_response_agent` | Emergency response planning | 9104 |
| `geoSpatial_agent` | Photo geolocation + incident classification | 9105 |
| `intelligence_agent` | Situational awareness + threat assessment | 9105 |
| `coding_agent` | Code writing via mini-swe-agent | 9106 |
| `scout_agent` | OSINT web research | 9104 |

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
  --card ../agent-directory/email_agent/email_agent.card.json \
  --card ../agent-directory/coding_agent/coding_agent.card.json
```

Open **http://localhost:8090/** for the chat UI.

The registration service has an analogous **fake-DB mode** (`FAKE_DB=1`) that stubs out postgres, ADS publishing, and subprocess spawning — useful for working on the registration UI and API without any external services:

```bash
python3 -m venv registration_service/.venv
registration_service/.venv/bin/pip install -r registration_service/requirements.txt
FAKE_DB=1 PYTHONPATH=. registration_service/.venv/bin/python -m registration_service.server
```

Open **http://localhost:8091/** for the registration wizard. See [`registration_service/README.md`](registration_service/README.md) for details including how to run its test suite.

## Environments

Each component is isolated — do not install one component's requirements into another's environment. They communicate only over HTTP (A2A), so dependency versions don't need to align.

| Environment | Components |
|---|---|
| pip venv | `planner_agent` |
| conda `agent-directory-service` | `agent_directory_service` |
| pip venv | `registration_service` |

Specialist agent environments live in the [`agent-directory`](https://github.com/washu-dev/agent-directory) repo.

## Agent Cards

Each agent owns its card — a JSON file that describes its name, endpoint URL, and skills. Cards live next to the agent code in the `agent-directory` repo and are published to ADS on startup.

The planner uses ADS to discover agents at runtime. In fake-ADS mode for local dev, pass card paths directly with `--card` (pointing into a checkout of `agent-directory`).

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
