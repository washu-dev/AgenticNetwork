# ENGR Cluster Deployment

This directory contains everything needed to run the full AgenticNetwork multi-agent
system on the WashU Engineering HPC cluster (ENGR) using Slurm and Pyxis/enroot.

## Architecture

Each service runs in its **own Docker container**, all launched in parallel as
`srun --overlap` steps within a single Slurm CPU job. A separate Slurm job runs
the vLLM inference server on a GPU node.

```
[CPU node — submit.sbatch]
  srun step: amaankhan25/agenticnetwork-ads:v4              :8888
  srun step: amaankhan25/agenticnetwork-registration:latest :8091  (+ postgres)
  srun step: amaankhan25/agenticnetwork-planner:latest      :8090
  srun step: amaankhan25/agenticnetwork-scout:latest        :9104
  srun step: amaankhan25/agenticnetwork-email:latest        :9103
  srun step: amaankhan25/agenticnetwork-coding:latest       :19106
  srun step: amaankhan25/agenticnetwork-intelligence:latest :19107
  srun step: amaankhan25/agenticnetwork-disaster:latest     :19108
  srun step: amaankhan25/agenticnetwork-geospatial:latest   :19105

[GPU node — start_vllm.sbatch]
  enroot container (vllm/vllm-openai:latest)
    └── vLLM serving openai/gpt-oss-120b   :8100
```

All containers communicate over `localhost` on the same compute node.
The planner reaches vLLM via `OPENAI_BASE_URL=http://<gpu-node>:8100/v1`.

ADS starts first; all agent containers wait up to 60 seconds for it to be
reachable before publishing their cards and starting their A2A servers.

The registration container also runs a PostgreSQL 15 instance internally.
Its data directory is bind-mounted from `/engrfs/project/di2/postgres_data`
so registered agents survive job restarts.

---

## Files

| File | Purpose |
|---|---|
| `submit.sbatch` | Main Slurm job — launches all service containers (postgres, ADS, registration, planner + 6 specialists) in parallel |
| `start_vllm.sbatch` | GPU Slurm job — starts the vLLM inference server |
| `build_and_push.sh` | Builds/pushes the **4 infra images** (planner, ads, postgres, registration); the 6 specialist images build in the `agent-directory` repo |
| `dockerfiles/` | One `Dockerfile.<agent>` per service |
| `entrypoints/` | One `entrypoint-<agent>.sh` per service (postgres init + ADS wait + exec) |
| `ads_daemon.yaml` | dirctl daemon config (listen address, data paths) |
| `google_credentials/` | OAuth credentials for the Email agent |
| `AGENT_INTEGRATION_SPEC.md` | Reference for adding a new agent: what fields are required and why |
| `DOWNSTREAM_CONSIDERATIONS.md` | Forward-looking architecture & scaling decisions (deferred-by-design) |

---

## One-time Setup

### 1. Create required directories on the cluster

```bash
mkdir -p /engrfs/project/di2/AgenticNetwork/logs
mkdir -p /engrfs/project/di2/postgres_data   # persistent postgres storage for registration service
```

### 2. Create your `.env` file

```bash
cp deployment/.env.example deployment/.env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `OPENAI_BASE_URL` | Set after starting vLLM — e.g. `http://gpu123:8100/v1` |
| `BRAVE_API_KEY` | Brave Search API key for Scout |
| `EMAIL_AGENT_EMAIL` | Gmail address for the Email agent |
| `GOOGLE_CALENDAR_ID` | Google Calendar ID |
| `GOOGLE_CALENDAR_TOKEN` | Path to `token.json` inside the container |
| `GOOGLE_CALENDAR_CREDENTIALS` | Path to `credentials.json` inside the container |
| `GOOGLE_API_KEY` / `GOOGLE_MAPS_API_KEY` | For the GeoSpatial agent |
| `PGDATABASE` | Postgres database name for the registration service (default: `registration`) |
| `REGISTRATION_SERVICE_PORT` | Port for the registration service (default: `8091`) |

Edit `submit.sbatch` and `start_vllm.sbatch` — replace `engr-lab-<piname>` with
your actual PI's account name (find it with `sacctmgr show user $USER`).

---

## Building Images

Run from the repo root on your local machine:

```bash
# Build and push the 4 infra images (planner, ads, postgres, registration).
# NOTE: the 6 specialist agent images are built in the agent-directory repo,
# not here (agent-directory/build_and_push.sh).
bash deployment/build_and_push.sh

# Build a single image (e.g. after changing only the planner)
bash deployment/build_and_push.sh planner
```

Images are published to `amaankhan25/agenticnetwork-<agent>:latest` on DockerHub.
Pyxis caches images by tag — use a new tag (`:v2`, `:v3`, etc.) if you need to
force a fresh pull on the cluster after pushing a fix to the same tag.

> **Note:** The ADS image is currently pinned to `:v4` in `submit.sbatch` rather
> than `:latest`. Update the tag in `submit.sbatch` if you rebuild and push the ADS image.

---

## Running on the Cluster

### Step 1 — Start the vLLM inference server

```bash
sbatch deployment/start_vllm.sbatch
```

Find the GPU node hostname and update `.env`:

```bash
squeue -u $USER   # note NODELIST, e.g. gpu123
# Then set: OPENAI_BASE_URL=http://gpu123:8100/v1
```

Verify vLLM is ready (from a compute node, not the login node):

```bash
curl http://gpu123:8100/health
# Expected: {"status":"ok"}
```

### Step 2 — Submit the main job

```bash
sbatch deployment/submit.sbatch
```

Watch startup:

```bash
tail -f /engrfs/project/di2/AgenticNetwork/logs/agenticnetwork-<jobid>.log
```

ADS should be ready within ~10 seconds; all agents should be running within ~60 seconds.
The registration container takes a few extra seconds on first run to initialise the
postgres data directory.

---

## Testing

All curl commands must be run **from the compute node** running the job (not the login node):

```bash
ssh <compute-node>   # e.g. ssh amd128-2204
```

### Health check

```bash
curl http://localhost:8090/health
# Expected: {"status":"ok","fake_ads":"False","model":"openai/gpt-oss-120b"}
```

### Registration service

```bash
curl http://localhost:8091/health
# Expected: {"status":"ok"}
```

The registration web UI is also accessible via the planner at `http://localhost:8090/register/`.

### List available agents

```bash
curl -s http://localhost:8090/messages \
  -H "Content-Type: application/json" \
  -d '{"text": "List all available agents."}' | python3 -m json.tool
```

A successful response has `"state": "completed"` and lists all specialist agents.

### Send a task to an agent

```bash
curl -s http://localhost:8090/messages \
  -H "Content-Type: application/json" \
  -d '{"text": "Give me a brief situational summary about wildfire preparedness."}' \
  | python3 -m json.tool
```

---

## Logs

All logs are written to `/engrfs/project/di2/AgenticNetwork/logs/` and persist
after the job exits. The main job log is `agenticnetwork-<jobid>.log`.

| File | Contents |
|---|---|
| `agenticnetwork-<jobid>.log` | Job-level startup and ADS readiness output |
| `ads.log` / `ads.err` | ADS (dirctl) daemon |
| `registration.log` / `registration.err` | Registration service + embedded postgres |
| `planner.log` / `planner.err` | Planner agent |
| `scout.log` / `scout.err` | Scout agent |
| `email.log` / `email.err` | Email agent |
| `coding.log` / `coding.err` | Coding agent |
| `intelligence.log` / `intelligence.err` | Intelligence agent |
| `disaster.log` / `disaster.err` | Disaster Response agent |
| `geospatial.log` / `geospatial.err` | GeoSpatial agent |
| `vllm-<jobid>.log` / `.err` | vLLM server |

---

## Port Reference

| Service | Port | Env var |
|---|---|---|
| ADS daemon | 8888 | `ADS_SERVER_ADDR` |
| Registration service | 8091 | `REGISTRATION_SERVICE_PORT` |
| Planner | 8090 | `MAIN_AGENT_PORT` |
| Scout | 9104 | `SCOUT_HTTP_PORT` |
| Email | 9103 | `EMAIL_AGENT_A2A_PORT` |
| GeoSpatial | 19105 | `GEOSPATIAL_AGENT_A2A_PORT` |
| Coding | 19106 | `CODING_AGENT_A2A_PORT` |
| Intelligence | 19107 | `INTEL_AGENT_A2A_PORT` |
| Disaster Response | 19108 | `DISASTER_AGENT_A2A_PORT` |
| vLLM | 8100 | `OPENAI_BASE_URL` |

Ports 19105–19108 use 5-digit numbers to avoid collisions with other users on shared nodes.

---

## Troubleshooting

**Connection refused on the login node**
Curl must be run from the compute node running the job. Agent ports are bound to
`localhost` inside each container and are not accessible from the login node.

**ADS not ready / agents fail to register**
Check `ads.log`. If it is empty or shows repeated errors, the dirctl binary may
have failed to start. Verify the ADS image is up to date (use a new tag to bypass
Pyxis cache; the ADS image is currently pinned to `:v4` in `submit.sbatch`).

**Registration postgres fails to start**
Check `registration.err`. Common causes: the `/engrfs/project/di2/postgres_data`
directory doesn't exist (create it — see One-time Setup), or a previous run left a
stale `postmaster.pid` (the entrypoint removes it automatically on start, but if
the data directory is corrupted, delete it and let `initdb` re-initialise on next run).

**`ModuleNotFoundError` or wrong Python version in agent logs**
The cluster auto-mounts its system anaconda (Python 3.7) into containers. All
entrypoints use absolute paths to the conda env Python to avoid this. If you see
this error, check that the relevant entrypoint script still uses the full path.

**vLLM returns 404 or connection errors**
Confirm the GPU job is still running (`squeue -u $USER`) and that `OPENAI_BASE_URL`
in `.env` matches the current GPU node hostname. GPU jobs time out and the node
hostname changes on each new submission.

**Pyxis serving a stale image after a fix**
Pyxis caches images by name and tag. Push to a new tag (e.g. `:v5`) and update
the image reference in `submit.sbatch` to force a fresh pull.

**Port conflict with another user's job**
Change the conflicting port via the corresponding env var in `.env`
(e.g. `DISASTER_AGENT_A2A_PORT=29107`). No rebuild needed.

---

## Running the network from your laptop (SSH tunnel to cluster vLLM)

You don't have to run the whole stack on the cluster. A common dev loop is: **keep
heavy LLM inference on the cluster GPU (vLLM), run the lightweight planner (and,
optionally, specialists) on your laptop**, bridging to vLLM over an SSH tunnel.

1. **Start vLLM on the cluster** and note its GPU node:
   ```bash
   ssh <you>@<login-node>
   sbatch deployment/start_vllm.sbatch
   squeue -u $USER            # note NODELIST, e.g. gpu123
   ```
2. **Open an SSH tunnel** so your laptop's `localhost:8100` reaches the GPU node's vLLM:
   ```bash
   ssh -N -L 8100:gpu123:8100 <you>@<login-node>
   # leave running; if gpu123 isn't reachable from the login node, add a jump:
   #   ssh -N -L 8100:gpu123:8100 -J <you>@<login-node> <you>@gpu123
   ```
3. **Run the planner locally, pointed at the tunnel** (Tier A — fake directory, no `dirctl`):
   ```bash
   python3 -m venv planner_agent/.venv
   planner_agent/.venv/bin/pip install -r planner_agent/requirements.txt
   cp planner_agent/.env.example planner_agent/.env.local
   # in .env.local:
   #   OPENAI_BASE_URL=http://localhost:8100/v1
   #   OPENAI_API_KEY=dummy                     # vLLM ignores it, but it must be non-empty
   #   MAIN_AGENT_LLM_MODEL=openai/gpt-oss-120b
   planner_agent/scripts/start.sh --fake-ads \
     --card ../agent-directory/email_agent/email_agent.card.json \
     --card ../agent-directory/coding_agent/coding_agent.card.json
   ```
   Open **http://localhost:8090/**. Agents are *discovered* from the `--card` files;
   each only *executes* if that specialist is actually running/reachable.
4. **(Optional) full local network** — instead of `--fake-ads`, install `dirctl`, run a
   real ADS daemon on `:8888`, and launch each specialist locally pointing at it
   (the one vLLM tunnel serves them all). See repo-root `CLAUDE.md` §4 Tier B.

**The inverse** (network on the cluster, browser on your laptop) is just another tunnel
to the planner — this bypasses Shibboleth and hits the planner directly:
```bash
ssh -N -L 8090:<compute-node>:8090 <you>@<login-node>
# then open http://localhost:8090/
```

---

## How the public demo link works (`agenticweb.engr.wustl.edu`)

The "click a link and the demo just works" experience is **not** the cluster job
talking to your browser directly — the cluster binds every port to `localhost` on an
ephemeral compute node. A persistent **gateway** in front turns it into a stable,
authenticated URL:

```
Your browser
  │  https://agenticweb.engr.wustl.edu
  ▼
nginx              ← TLS termination, request-size limits    ┐
  │                                                          │  persistent WashU ENGR
Apache + mod_shib  ← Shibboleth SP: WashU SSO login,         │  gateway host
  │                  injects eppn/mail/remote-user headers   │  (NOT in this repo)
  ▼                                                          ┘
planner :8090      ← trusts those headers as the user id       ← ephemeral Slurm node
  │
  ├─ ADS (dirctl :8888) ...... "who can do X?"  (discovery)
  └─ A2A (HTTP/JSON-RPC) ...... calls the chosen specialist agent
```

- **Where the network runs:** on one Slurm **compute node** (`submit.sbatch`) — ADS,
  planner, registration, and the six specialists all talk over `localhost` on that
  node; vLLM runs on a separate GPU node. None of it is internet-reachable.
- **How the browser reaches it:** the gateway host terminates TLS (nginx), runs the
  WashU Shibboleth login (Apache + `mod_shib`), and reverse-proxies to the planner's
  `:8090`. The planner's only "auth" is trusting the identity headers Apache injects
  (`planner_agent/main_agent/auth.py`) — so it must **only** be reached through that
  proxy, never exposed raw.
- **Where the link comes from:** `agenticweb.engr.wustl.edu` is a WashU-registered DNS
  name pointing at the gateway host; the Shibboleth SP on it is registered with WashU's
  identity provider (a one-time WashU-IT/ENGR setup owned by the maintainer —
  `amaan@wustl.edu`). **The gateway's nginx/Apache/Shibboleth config is not in this
  repo** — only the *contract* (the injected headers) lives here, in `auth.py`.
- **Why the link can break:** the planner runs on an ephemeral compute node whose
  hostname changes every Slurm run, while the gateway is persistent — so after each
  (re)deploy the gateway's upstream must be repointed to the current node (or an SSH
  tunnel bridged to it). There is **no stable gateway↔node wiring yet**
  (`DOWNSTREAM_CONSIDERATIONS.md` #3).
- **Local shortcut:** an `ADMIN_PASSWORD` + `POST /admin/login` fallback lets you use
  the planner without Shibboleth for local dev (`auth.py`).

---

## Security & hazards

See [`../SECURITY.md`](../SECURITY.md) for the current security posture, known
hazards, and the hardening checklist (no auth on specialist ports, the uploaded-code
trust boundary, `coding_agent` YOLO bash, credential handling, stored-but-not-routed).
