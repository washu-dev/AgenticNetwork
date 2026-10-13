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
| [`registration_service/`](registration_service/) | Agent self-registration web UI + manager | 8091 | [README](registration_service/README.md) |
| [`agent_skeleton/`](agent_skeleton/) | Agent template + endpoint-wrapper engine + generator | — | [README](agent_skeleton/README.md) |
| [`deployment/`](deployment/) | Deploy machinery: ENGR cluster (live) and GCP Cloud Run | — | [README](docs/deployment/README.md) |

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

## Run it locally

One command runs the whole system on a laptop: the planner, the registration service, a seed agent, a mock LLM and a local sign-in proxy. From the repo root:

```bash
# one-time setup: the two service venvs
python3 -m venv planner_agent/.venv && planner_agent/.venv/bin/pip install -r planner_agent/requirements.txt
python3 -m venv registration_service/.venv && registration_service/.venv/bin/pip install -r registration_service/requirements.txt

scripts/stack up                    # returns when everything is ready
open http://localhost:8080/app      # chat UI, signed in as dev@example.test
scripts/stack down
```

No Docker, database, SSO or API key; the LLM is an offline mock by default. Full guide (a real LLM, adding agents, logs, troubleshooting): [`docs/DEVSTACK.md`](docs/DEVSTACK.md). Component-only dev modes (the planner's `--fake-ads`, registration's `FAKE_DB=1`) are in each component's README.

## Deployment

The live system runs on the WashU **ENGR cluster** via Slurm + Pyxis/enroot. Each component is a Docker image; `deployment/submit.sbatch` starts them all as parallel containers on one node, with ADS coming up first and agents self-publishing on startup. See [`docs/deployment/engr/README.md`](docs/deployment/engr/README.md) for the cluster runbook.

Cloud is a critical leg of the project going forward (decided 2026-10-13). A GCP Cloud Run deployment (planner, registration service, on-demand agents) was built to a working milestone in summer 2026 and is on hold; see [`docs/deployment/gcp/README.md`](docs/deployment/gcp/README.md).

## Environments

Each component is isolated — do not install one component's requirements into another's environment. They communicate only over HTTP (A2A), so dependency versions don't need to align.

| Environment | Components |
|---|---|
| pip venv | `planner_agent` |
| pip venv | `agent_directory_service` |
| pip venv | `registration_service` |

Specialist agent environments live in the [`agent-directory`](https://github.com/washu-dev/agent-directory) repo.

## Agent Cards

Each agent owns its card — a JSON file that describes its name, endpoint URL, and skills. Cards live next to the agent code in the `agent-directory` repo and are published to ADS on startup.

The planner uses ADS to discover agents at runtime.

## Adding a new agent

Register one through the registration service (no code for `llm` and `proxy` agents; upload code for `custom`), or build a standalone A2A agent that publishes its own card. Either way the planner discovers it automatically. See [`docs/BUILDING_AGENTS.md`](docs/BUILDING_AGENTS.md); in the dev stack, `scripts/stack add-agent FILE.json` registers one locally.

## Further reading

- [`docs/`](docs/README.md) — **developer reference**: architecture, auth & credentials, per-subsystem deep-dives, and the decision log
- [`CLAUDE.md`](CLAUDE.md) — system guide + operating manual (run tiers, LLM/vLLM config, caveats)
- [`SECURITY.md`](SECURITY.md) — security posture + active hazards
- [`AGENTS.md`](AGENTS.md) — working rules and validation commands for contributors
- [`docs/DEVSTACK.md`](docs/DEVSTACK.md) — the one-command local stack
- [`docs/OPEN_SOURCE_RELEASE.md`](docs/OPEN_SOURCE_RELEASE.md) — open-source release plan and status
- [`docs/deployment/engr/README.md`](docs/deployment/engr/README.md) — ENGR cluster runbook
