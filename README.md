# AgenticNetwork

A demo of an **"Internet of Agents"**: independent, LLM-powered services that discover each other through a shared directory and collaborate over a standard agent-to-agent protocol. A user talks to one orchestrator (the **planner**); the planner discovers specialist agents at runtime and delegates tasks to them — with no hardcoded knowledge of which agents exist.

## Using the deployed system

The live system is at **https://agenticweb.engr.wustl.edu**.

1. **Sign in.** Open the site and click **Sign in with WashU** — it uses WashU SSO (Shibboleth); there's no separate password.
2. **Chat.** Describe a task in plain language ("summarize wildfire preparedness," "send an email to…"). The planner figures out which specialist agent(s) can do it and delegates — you don't pick agents yourself.
3. **Connect your own accounts/keys** in **Settings → Credentials**. Some agents act on *your* resources (e.g. your Google account) or need an API key. Keys are encrypted, and the planner shares a key only with agents **you've granted** it to, only for **your** requests — you're asked to allow an agent the first time it needs one.
4. **Admins** (WashU emails in the `ADMIN_EMAILS` allowlist) also get a dashboard — agent health, request traces, and approval of uploaded custom agents. Everyone else never sees admin surfaces.

Building or operating the system rather than using it? See the **developer reference** in [`docs/`](docs/README.md).

## How it works

A user talks to the **planner** (`:8090`), which discovers specialist agents via the **ADS**
registry (`:8888`) and dispatches tasks to them over **A2A** (HTTP/JSON-RPC). Adding a new agent
to ADS makes it available to the planner with **zero planner code changes**. For the architecture
diagram and the three contracts (Agent Card / ADS / A2A) that hold this together, see
[`CLAUDE.md`](CLAUDE.md) §1–§2.

## Components

**This repo — infrastructure:**

| Directory | Role | Port | Full docs |
|---|---|---|---|
| [`planner_agent/`](planner_agent/) | Orchestrator + chat UI | 8090 | [README](planner_agent/README.md) |
| [`agent_directory_service/`](agent_directory_service/) | ADS operator tooling (cluster ops) | — | [README](agent_directory_service/README.md) |
| [`registration_service/`](registration_service/) | Agent self-registration web UI + manager | — | [README](registration_service/README.md) |
| [`agent_skeleton/`](agent_skeleton/) | Agent template + endpoint-wrapper engine + generator | — | [README](agent_skeleton/README.md) |
| [`deployment/`](deployment/) | ENGR cluster containerized deployment | — | [README](docs/deployment/engr/README.md) |

**[`agent-directory`](https://github.com/washu-dev/agent-directory) repo — specialist agents:**

| Agent | Role | Port |
|---|---|---|
| `email_agent` | Gmail, Calendar, Docs, Sheets | 9103 |
| `scout_agent` | OSINT web research | 9104 |
| `geoSpatial_agent` | Photo geolocation + incident classification | 19105 |
| `coding_agent` | Code writing via mini-swe-agent | 19106 |
| `intelligence_agent` | Situational awareness + threat assessment | 19107 |
| `disaster_response_agent` | Emergency response planning | 19108 |

> **5-digit ports (19105–19108)** avoid collisions with other users on shared ENGR cluster nodes. Override via the corresponding `*_PORT` env vars in `deployment/.env`.

## Deployment

The system runs on the WashU **ENGR cluster** via Slurm + Pyxis/enroot. Each component is a Docker image; `deployment/submit.sbatch` starts them all as parallel containers on one node, with ADS coming up first and agents self-publishing on startup.

See [`deployment/README.md`](docs/deployment/engr/README.md) for the full cluster runbook.

For local development without the cluster, the planner supports a **fake-ADS mode** that runs an
in-process directory — useful for testing the planner against specific agent cards without
standing up the full stack. See [`CLAUDE.md`](CLAUDE.md) §4 "Tier A" for the setup commands; it
ends with the chat UI at **http://localhost:8090/**.

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

See [`docs/BUILDING_AGENTS.md`](docs/BUILDING_AGENTS.md) for the full step-by-step guide (and the upload / endpoint-wrapper lanes).

## Further reading

- [`docs/`](docs/README.md) — **developer reference**: architecture, auth & credentials, per-subsystem deep-dives, and the decision log
- [`CLAUDE.md`](CLAUDE.md) — system guide + operating manual (run tiers, LLM/vLLM config, caveats)
- [`SECURITY.md`](SECURITY.md) — security posture + active hazards
- [`AGENTS.md`](AGENTS.md) — working rules and validation commands for contributors
- [`deployment/README.md`](docs/deployment/engr/README.md) — ENGR cluster runbook
