# ENGR Cluster Deployment

This directory contains everything needed to run the full AgenticNetwork multi-agent
system on the WashU Engineering HPC cluster (ENGR) using Slurm and enroot.

## Architecture

All eight services run inside a **single enroot container** on one CPU compute node.
A separate Slurm job runs the vLLM inference server on a GPU node.

```
[CPU node — submit.sbatch]
  enroot container (amaakhan25/agenticnetwork:latest)
    ├── dirctl  ADS daemon          :8888
    ├── planner Planner agent       :8090
    ├── scout   Scout agent         :9104
    ├── email   Email agent         :9103
    ├── coding  Coding agent        :9106
    ├── intel   Intelligence agent  :9105
    ├── disaster Disaster Response  :19107
    └── geospatial GeoSpatial       :19108

[GPU node — start_vllm.sbatch]
  enroot container (vllm/vllm-openai:latest)
    └── vLLM serving openai/gpt-oss-20b   :8100
```

All services communicate over `localhost` inside the container.
The planner reaches vLLM via `OPENAI_BASE_URL=http://<gpu-node>:8100/v1`.

---

## One-time Setup

### 1. Create required directories on the cluster

```bash
mkdir -p /engrfs/project/di2/AgenticNetwork/logs
mkdir -p /engrfs/project/di2/AgenticNetwork/hf_cache
```

### 2. Create your `.env` file

```bash
cp deployment/.env.example deployment/.env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `OPENAI_BASE_URL` | Set after starting vLLM — see step below |
| `BRAVE_API_KEY` | Brave Search API key for Scout |
| `EMAIL_AGENT_EMAIL` | Gmail address for the Email agent |
| `GOOGLE_CALENDAR_ID` | Google Calendar ID |
| `GOOGLE_CALENDAR_TOKEN` | Path to `token.json` inside the container |
| `GOOGLE_CALENDAR_CREDENTIALS` | Path to `credentials.json` inside the container |
| `GOOGLE_API_KEY` / `GOOGLE_MAPS_API_KEY` | For the GeoSpatial agent |

Edit `submit.sbatch` and `start_vllm.sbatch` — replace `engr-lab-<piname>` with
your actual PI's account name (find it with `sacctmgr show user $USER`).

---

## Running on the Cluster

### Step 1 — Start the vLLM inference server

```bash
sbatch deployment/start_vllm.sbatch
```

Wait for the job to start, then find the GPU node hostname:

```bash
squeue -u $USER
# Note the NODELIST value, e.g. gpu123
```

Update `OPENAI_BASE_URL` in your `.env`:

```
OPENAI_BASE_URL=http://gpu123:8100/v1
```

Verify vLLM is ready (run from a compute node, not the login node):

```bash
curl http://gpu123:8100/health
# Expected: {"status":"ok"}
```

### Step 2 — Submit the main container job

```bash
sbatch deployment/submit.sbatch
```

Check the job started:

```bash
squeue -u $USER
# Note the NODELIST value, e.g. amd128-2204
```

Watch startup progress:

```bash
tail -f /engrfs/project/di2/AgenticNetwork/logs/supervisord.log
```

All seven agents should report `entered RUNNING state` within ~60 seconds.

---

## Testing

All curl commands must be run **from the compute node** running the job
(not the login node). SSH there first:

```bash
ssh <compute-node>   # e.g. ssh amd128-2204
```

### Health check

```bash
curl http://localhost:8090/health
# Expected: {"status":"ok","fake_ads":"False","model":"openai/gpt-oss-20b"}
```

### List available agents

```bash
curl -X POST http://localhost:8090/messages \
  -H "Content-Type: application/json" \
  -d '{"text": "List all available agents."}'
```

### Send a task to an agent

```bash
curl -X POST http://localhost:8090/messages \
  -H "Content-Type: application/json" \
  -d '{"text": "What is the current weather situation in St. Louis?"}'
```

---

## Logs

All logs are written to `/engrfs/project/di2/AgenticNetwork/logs/` and persist
after the job exits.

| File | Contents |
|---|---|
| `supervisord.log` | Process lifecycle (start/stop/crash) |
| `planner.err` | Planner agent stderr (LLM calls, errors) |
| `planner.log` | Planner agent stdout |
| `scout.err` / `scout.log` | Scout agent |
| `email.err` / `email.log` | Email agent |
| `coding.err` / `coding.log` | Coding agent |
| `intelligence.err` / `intelligence.log` | Intelligence agent |
| `disaster.err` / `disaster.log` | Disaster Response agent |
| `geospatial.err` / `geospatial.log` | GeoSpatial agent |
| `ads.log` | ADS (dirctl) daemon |
| `vllm-<jobid>.log` | vLLM server stdout |
| `vllm-<jobid>.err` | vLLM server stderr |

---

## Port Reference

| Service | Port | Env var |
|---|---|---|
| ADS daemon | 8888 | `ADS_SERVER_ADDR` |
| Planner | 8090 | `MAIN_AGENT_PORT` |
| Scout | 9104 | `SCOUT_HTTP_PORT` |
| Email | 9103 | `EMAIL_AGENT_A2A_PORT` |
| Coding | 9106 | `CODING_AGENT_A2A_PORT` |
| Intelligence | 9105 | `INTEL_AGENT_A2A_PORT` |
| Disaster Response | 19107 | `DISASTER_AGENT_A2A_PORT` |
| GeoSpatial | 19108 | `GEOSPATIAL_AGENT_A2A_PORT` |
| vLLM | 8100 | `OPENAI_BASE_URL` |

Ports 19107 and 19108 use 5-digit numbers to avoid collisions with other users on shared nodes.

---

## Troubleshooting

**Connection refused on the login node**
Curl must be run from the compute node running the job. The planner binds to
`localhost` inside the container, not to the cluster network.

**ADS not ready / agents fail to register**
Check `ads.log`. The entrypoint waits up to 30 seconds for `"Daemon ready"` before
starting supervisord. If the log is empty, the `dirctl` binary may have failed —
verify `/usr/local/bin/dirctl` exists inside the container.

**vLLM returns 404 or connection errors**
Confirm the GPU job is still running (`squeue -u $USER`) and that `OPENAI_BASE_URL`
in `.env` matches the current GPU node hostname. GPU jobs time out after 4 hours.

**Conda environments rebuilding on every Docker build**
Each agent's dependency files are copied individually before `COPY . .` in the
Dockerfile. If a `requirements.txt` or `environment.yml` changes, only that
agent's layer rebuilds. If all layers rebuild, verify the `COPY` order in the
Dockerfile has not been changed.

**Port conflict with another user's job**
Change the conflicting port in `.env` (e.g. `DISASTER_AGENT_A2A_PORT=29107`) and
update the corresponding comment in `supervisord.conf`. No rebuild needed.
