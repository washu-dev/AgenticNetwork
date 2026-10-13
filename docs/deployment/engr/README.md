# ENGR Cluster Deployment

> The **GCP** deployment of the same system is documented in [../gcp/README.md](../gcp/README.md) —
> read the two side-by-side to compare. The build/run **machinery** (Dockerfiles, entrypoints, launch
> scripts) lives in the repo-root [`deployment/`](../../../deployment/).

This runbook covers running the full AgenticNetwork multi-agent system on the WashU Engineering HPC
cluster (ENGR) using Slurm and Pyxis/enroot. ENGR is the live public deployment
(`agenticweb.engr.wustl.edu`).

## Architecture

Each service runs in its **own Docker container**, all launched in parallel as
`srun --overlap` steps within a single Slurm CPU job. A separate Slurm job runs
the vLLM inference server on a GPU node.

```
[CPU node — submit.sbatch]
  srun step: di2washu/agenticnetwork-postgres:latest     :5432
  srun step: di2washu/agenticnetwork-ads:v4              :8888
  srun step: di2washu/agenticnetwork-registration:latest :8091
  srun step: di2washu/agenticnetwork-planner:latest      :8090
  srun step: di2washu/agenticnetwork-scout:latest        :9104
  srun step: di2washu/agenticnetwork-email:latest        :9103
  srun step: di2washu/agenticnetwork-coding:latest       :19106
  srun step: di2washu/agenticnetwork-intelligence:latest :19107
  srun step: di2washu/agenticnetwork-disaster:latest     :19108
  srun step: di2washu/agenticnetwork-geospatial:latest   :19105

[GPU node — start_vllm.sbatch]
  enroot container (vllm/vllm-openai:latest)
    └── vLLM serving openai/gpt-oss-120b   :8100
```

All containers communicate over `localhost` on the same compute node.
The planner reaches vLLM via `OPENAI_BASE_URL=http://<gpu-node>:8100/v1`.

Postgres starts first (ADS and the registration service both depend on it),
then ADS; all agent containers wait up to 60 seconds for ADS to be reachable
before publishing their cards and starting their A2A servers.

PostgreSQL runs in its own dedicated container (`Dockerfile.postgres` /
`entrypoint-postgres.sh`), serving two databases on `localhost:5432`: the
persistent `registration` DB — bind-mounted from
`/engrfs/project/di2/postgres_data` so registered agents survive job restarts —
and the ephemeral `dir` DB that ADS uses (dropped + recreated each run). The
registration and ADS containers are pure DB clients.

---

## Files

| File | Purpose |
|---|---|
| `submit.sbatch` | Main Slurm job — launches all service containers (postgres, ADS, registration, planner + 6 specialists) in parallel |
| `start_vllm.sbatch` | GPU Slurm job — starts the vLLM inference server |
| `build_and_push.sh` | Builds/pushes the **4 infra images** (planner, ads, postgres, registration); the 6 specialist images build in the `agent-directory` repo |
| `dockerfiles/` | One `Dockerfile.<agent>` per infra service (ADS runs `dirctl` directly, no Dockerfile-side entrypoint script) |
| `entrypoints/` | `entrypoint-{postgres,registration,planner}.sh` (postgres init / DB + ADS wait / exec); ADS has none — it runs `dirctl daemon start` directly from `submit.sbatch` |
| `ads_daemon.yaml` | dirctl daemon config (listen address, Postgres backend, data paths) |

The full artifact list is in [`deployment/README.md`](../../../deployment/README.md). Related docs:
[`AGENT_INTEGRATION_SPEC.md`](../AGENT_INTEGRATION_SPEC.md) (what an agent must provide and why) ·
[`DOWNSTREAM_CONSIDERATIONS.md`](../DOWNSTREAM_CONSIDERATIONS.md) (deferred scaling decisions).

---

## One-time Setup

### 1. Create required directories on the cluster

```bash
mkdir -p /engrfs/project/di2/AgenticNetwork/logs/$USER   # Slurm writes the job logs here (logs/%u)
mkdir -p /engrfs/project/di2/postgres_data   # persistent postgres storage for registration service
```

### 2. Create your `.env` file

```bash
cp deployment/.env.example deployment/.env
```

Edit `.env` — the values you may actually change (most have working defaults):

| Variable | Description |
|---|---|
| `ADMIN_EMAILS` | Comma-separated allowlist of admin emails, matched against SSO identity |
| `GOOGLE_CLIENT_ID` / `GOOGLE_REDIRECT_URI` | OAuth web-client (non-secret half; the secret is in the secrets file, Step 3) |
| `GITHUB_REPO` | Repo for the custom-agent build pipeline (default `washu-dev/agent-directory`) |
| `IMAGE_REGISTRY` | Image namespace (default `di2washu`; see [`docs/MULTI_CONTRIBUTOR.md`](../../MULTI_CONTRIBUTOR.md)) |
| `PGDATABASE` | Registration DB name (default `registration`) |

> `.env` holds **non-secret config only** — it is shared and world-readable on `/engrfs`.
>
> **You no longer set these by hand:** `OPENAI_BASE_URL` is auto-discovered (Step 1); real
> secrets go in the secrets file (Step 3); and per-user credentials — `BRAVE_API_KEY`,
> `GOOGLE_API_KEY`/`GOOGLE_MAPS_API_KEY`, the Email agent's Gmail/Calendar tokens — are stored
> **per user** via the app's credential flow (the Google OAuth connect + `/me/credentials`),
> not in env. Ports are derived by `submit.sbatch` (offset for a test instance).

### 3. Create your secrets file

Real secrets (`CREDENTIALS_ENCRYPTION_KEY`, `GITHUB_TOKEN`, `GITHUB_ISSUES_TOKEN`,
`GOOGLE_CLIENT_SECRET`, `VLLM_API_KEY`) live in a restricted file, **not** `.env`. `submit.sbatch` sources it
after `.env`. Copy the template and set restrictive permissions:

```bash
# prod (shared among authorized operators; lives beside the .example, gitignored as secrets.env):
cp deployment/secrets.env.example /engrfs/project/di2/AgenticNetwork/deployment/secrets.env
chmod 0640 /engrfs/project/di2/AgenticNetwork/deployment/secrets.env    # then fill in real values

# test (per-user, private):
mkdir -p ~/.config/agenticnetwork && chmod 0700 ~/.config/agenticnetwork
cp deployment/secrets.env.example ~/.config/agenticnetwork/secrets.env
chmod 0600 ~/.config/agenticnetwork/secrets.env
```

Override the path with `SECRETS_FILE=/path/to/secrets.env`. See `secrets.env.example` for details.
The prod file **must** be named `secrets.env` — that name is gitignored (`**/secrets.env`), so it
is never committed even though it sits inside the repo tree.

> **Bug reporting & feedback:** `GITHUB_ISSUES_TOKEN` (secret) + `GITHUB_ISSUES_REPO` (non-secret,
> in `.env`, default `washu-dev/AgenticNetwork`) power the planner's `POST /issues`, which backs
> the in-app "Report a bug" button and the agent-creation feedback form. Both file GitHub issues
> labeled `user-submitted` + `bug-report`/`agent-creation-feedback`. If the token is unset the
> forms fail loud (HTTP 502) — they never silently drop a report. The registration UI's feedback
> form reaches this endpoint only when served **through the planner** (`/register/…`), which is
> the deployed path. The token and egress from a cluster node are not yet recorded as tested; see
> [Verifying bug reporting](#verifying-bug-reporting-not-yet-recorded-as-done) before adding the
> token to prod.

The vLLM HuggingFace token lives in a separate file (`/workspace/.hf_token`, read by
`start_vllm.sbatch`) — it's also a secret on shared storage, so `chmod 600` it.

### 4. Check your Slurm account

`submit.sbatch` and `start_vllm.sbatch` charge `engr-lab-di2` (`#SBATCH -A`). Change it in both
if you run under a different PI account (find yours with `sacctmgr show user $USER`).

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

Images are published to `<IMAGE_REGISTRY>/agenticnetwork-<agent>:latest`, where
`IMAGE_REGISTRY` defaults to `di2washu` and is overridable via the `IMAGE_REGISTRY`
env var (set it in `deployment/.env`) — `build_and_push.sh`, `submit.sbatch`, and the
registration service's custom-agent build all read it. Moving this off a personal
account is tracked in [`docs/MULTI_CONTRIBUTOR.md`](../../MULTI_CONTRIBUTOR.md).
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

The job runs as `agenticnetwork-vllm`. **You no longer hand-edit `OPENAI_BASE_URL`** —
`submit.sbatch` auto-discovers the vLLM node by that job name at launch and sets the URL
(preferring your own job, else any user's, so a test instance can share prod's vLLM). It
falls back to the `.env` value if no vLLM job is running; force that with
`VLLM_AUTODISCOVER=0`. Just confirm the job is `RUNNING` before Step 2:

```bash
squeue -u $USER -n agenticnetwork-vllm   # wait for state R (and note NODELIST for the check below)
```

Verify vLLM is ready (from a compute node, not the login node):

```bash
curl http://gpu123:8100/health
# Expected: {"status":"ok"}
```

### Step 2 — Submit the main job

Use the `deploy.sh` wrapper, which selects the instance:

```bash
deployment/deploy.sh          # prod — the canonical instance (default)
deployment/deploy.sh test     # an isolated test instance that can run BESIDE prod
```

**prod vs test.** prod is the one canonical deployment: base ports and the persistent
`/engrfs` Postgres holding real, encrypted user credentials. A `test` instance offsets every
port by **+10000** and puts its Postgres data on **node-local `/scratch/$USER`**, so it can run
concurrently with a live prod without colliding on ports and *cannot touch prod's data*. A
second deploy of the same instance (two prods, or two tests) is refused across all users by a
`squeue` preflight in `deploy.sh` (at submit time, so you get an immediate message and no job
stuck `PENDING`); an in-job preflight and `--dependency=singleton` back it up if you bypass the
wrapper. Details + known gaps: [`docs/MULTI_CONTRIBUTOR.md`](../../MULTI_CONTRIBUTOR.md).

> Submitting `sbatch deployment/submit.sbatch` directly still works and defaults to prod.

Watch startup:

```bash
tail -f /engrfs/project/di2/AgenticNetwork/logs/$USER/agenticnetwork-<jobid>.log
```

ADS should be ready within ~10 seconds; all agents should be running within ~60 seconds.
The postgres container takes a few extra seconds on first run to initialise its data
directory.

### Accessing a test instance (SSH tunnel)

A **prod** instance is reached at the public URL (`https://agenticweb.engr.wustl.edu`) via the
Apache + Shibboleth gateway. A **test** instance has **no public front door** — the gateway only
ever proxies to prod. You reach a test instance by tunnelling to its planner port
(`8090 + 10000 = 18090`) through the login node. All of this runs in **your laptop's terminal**,
not a shell already on the cluster.

**1. Find the test node** (in your normal cluster session):
```bash
squeue -u $USER -n agenticnetwork-test -h -o '%N'    # e.g. c2-gpu-013  → this is <test-node>
```

**2. Open the tunnel from your laptop** (two-hop: jump through the login node, SSH into the test
node, forward its local `:18090`). Use the same login host you always use for the cluster:
```bash
ssh -J <wustlkey>@<login-host> <wustlkey>@<test-node> -L 18090:localhost:18090
```
Approve the usual WashU password + DUO prompt. **Leave this SSH session open** — the tunnel lives
only while it's connected.

**3. Browse** `http://localhost:18090`. You land in the test UI as an anon/demo user
(`PLANNER_ALLOW_ANON=1` is set for `test` only; prod always keeps the SSO gate).

Notes:
- The test node **changes every deploy** — re-run step 1 and update `<test-node>` each time.
- If laptop port `18090` is busy, use a different left-side port: `-L 9000:localhost:18090`, then
  browse `localhost:9000`.
- **Google-connect won't complete in test** — `GOOGLE_REDIRECT_URI` points at the prod public URL,
  so that OAuth flow bounces back to prod. Core chat + non-Google agents work fine.
- Optional `~/.ssh/config` shortcut (then just `ssh agn-test` + browse `localhost:18090`):
  ```
  Host agn-test
      HostName <test-node>          # update when the node changes each deploy
      User <wustlkey>
      ProxyJump <wustlkey>@<login-host>
      LocalForward 18090 localhost:18090
  ```

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

`/health` is liveness only. `curl http://localhost:8090/ready` (and `:8091/ready`) also checks the
directory, the database and (planner only) the LLM; it returns 503 and names the failing check.

### Registration service

```bash
curl http://localhost:8091/health
# Expected: {"status":"ok"}
```

The registration web UI is also accessible via the planner at `http://localhost:8090/register/`.

### List available agents

Prod's planner has a database and no anonymous fallback, so `/messages` needs an identity header
(otherwise 401); a test instance (`PLANNER_ALLOW_ANON=1`) does not.

```bash
curl -s http://localhost:8090/messages \
  -H "Content-Type: application/json" -H "mail: <you>@wustl.edu" \
  -d '{"text": "List all available agents."}' | python3 -m json.tool
```

A successful response has `"state": "completed"` and lists all specialist agents.

### Send a task to an agent

```bash
curl -s http://localhost:8090/messages \
  -H "Content-Type: application/json" -H "mail: <you>@wustl.edu" \
  -d '{"text": "Give me a brief situational summary about wildfire preparedness."}' \
  | python3 -m json.tool
```

### Verifying bug reporting (not yet recorded as done)

The one piece of `POST /issues` not yet recorded as tested is the real token plus egress to
`api.github.com` from a cluster node (egress was reported available when the feature was planned,
2026-07-16). Prove it on a test instance before the token goes into prod's secrets.

1. *(Optional, local)* With a PAT that has Issues: write on a sandbox repo you own:
   `scripts/stack down`, then
   `DEVSTACK_PASSTHROUGH_ENV=GITHUB_ISSUES_TOKEN,GITHUB_ISSUES_REPO GITHUB_ISSUES_TOKEN=<pat> GITHUB_ISSUES_REPO=<you>/<sandbox> scripts/stack up`,
   and run the curl from step 3 against `http://localhost:8080`.
2. Put the token in your **per-user** secrets file (`~/.config/agenticnetwork/secrets.env`, Step 3),
   not the shared prod one, and run `deployment/deploy.sh test`. Open the tunnel from
   [Accessing a test instance](#accessing-a-test-instance-ssh-tunnel).
3. The egress test, through the tunnel:
   ```bash
   curl -s http://localhost:18090/issues -H 'Content-Type: application/json' \
     -d '{"kind":"bug","summary":"smoke test","description":"ignore me"}'
   # Expected: {"ok":true,"issue_url":"https://github.com/..."}
   ```
   A 502 is a failure. If the planner log says `could not reach GitHub`, the node has no egress:
   stop, because the GitHub-issues approach depends on it.
4. Check the issue in `GITHUB_ISSUES_REPO`: labels `user-submitted` + `bug-report`, and a readable
   "Auto-captured context" block. Submit the creation-feedback form at `/register/` and check for an
   `agent-creation-feedback` issue. Close both.
5. **Prod, only after 3-4 pass:** add `GITHUB_ISSUES_TOKEN` to the shared prod secrets file (Step 3),
   redeploy with `deployment/deploy.sh`, file one bug through the live UI, confirm it, then close it.

---

## Logs

All logs are written to `/engrfs/project/di2/AgenticNetwork/logs/$USER/` and persist
after the job exits. The main job log is `agenticnetwork-<jobid>.log`.

| File | Contents |
|---|---|
| `agenticnetwork-<jobid>.log` | Job-level startup and ADS readiness output |
| `postgres.log` | Dedicated PostgreSQL container (registration + dir DBs) |
| `ads.log` / `ads.err` | ADS (dirctl) daemon |
| `registration.log` / `registration.err` | Registration service |
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

## Apache / Shibboleth SP (SSO front door)

The planner sits behind an Apache reverse proxy with the Shibboleth SP, which authenticates
WashU users and injects `mail`/`eppn` headers. That vhost lives **outside this repo** (e.g.
`~/httpd/conf/agenticweb.engr.conf` on the proxy host).

**P4e (public landing):** the planner serves a public landing at `/` and the chat UI at `/app`.
For `/` to be reachable without login, the SP must allow it anonymously while still protecting
everything else. This is an **additive** change to the existing single `<Location "/">` block —
nothing needs to be removed:

```apache
# Add this so mod_shib's own endpoints are never proxied to the planner:
ProxyPass "/Shibboleth.sso" "!"

# Keep the existing protected base block (requireSession 1) exactly as-is, then add
# this AFTER it. Apache applies the later-matching section, so / and /health become
# public while /app, /messages, /me/*, /auth/*, /admin/* stay protected by the base.
<LocationMatch "^/(health)?$">
    ShibRequestSetting requireSession 0
    Require all granted
</LocationMatch>
```

Verify after reloading Apache:
```bash
curl -sI https://agenticweb.engr.wustl.edu:<port>/       # 200 (landing, no redirect)
curl -sI https://agenticweb.engr.wustl.edu:<port>/app    # 302 → WashU SSO
```
If `/` still redirects to SSO (authz didn't fully override), comment out `require valid-user`
+ `require shib-session` in the base `<Location "/">` and instead protect the specific paths
explicitly. The planner also hard-gates `/app` itself (401 without identity when a DB is
configured), so identity is enforced at both layers.

---

## Troubleshooting

**Connection refused on the login node**
Curl must be run from the compute node running the job. Agent ports are bound to
`localhost` inside each container and are not accessible from the login node.

**ADS not ready / agents fail to register**
Check `ads.log`. If it is empty or shows repeated errors, the dirctl binary may
have failed to start. Verify the ADS image is up to date (use a new tag to bypass
Pyxis cache; the ADS image is currently pinned to `:v4` in `submit.sbatch`).

**Postgres fails to start**
Check `postgres.log` (the dedicated postgres container: `Dockerfile.postgres`,
`entrypoint-postgres.sh`). Common causes: the `/engrfs/project/di2/postgres_data`
directory doesn't exist (create it — see One-time Setup), or a `postmaster.pid` from an
earlier run. The entrypoint removes a stale one itself, but refuses to start if its PID is
still alive (another Postgres owns the data dir, e.g. a double submit). If the data
directory is corrupted, delete it and let `initdb` re-initialise it on the next run.

**Custom agents don't auto-start on a fresh deployment (unlike the 6 core agents)**
Known limitation. The 6 core agents are hardcoded as `srun` steps in `submit.sbatch`,
so they always launch with the job. Custom agents are **not** in that static set — their
(re)start is driven entirely by the registration service's Postgres DB: on startup
`Supervisor.recover_live_agents` re-queues only rows with `status='live'` (and whose image
still exists on Docker Hub) to `image_ready`, and the host spawn loop (`spawn_agent_loop.py`)
then `srun`s them. Consequences:
- If the Postgres data dir is fresh/reset, or the custom agent's row isn't `status='live'`
  (e.g. it was `stopped`/`failed`), it will **not** come back up on the next deploy — even
  though its build context is committed in the `agent-directory` GitHub registry and its image
  is on Docker Hub. Recovery is **DB-driven, not registry-driven**: nothing enumerates the
  GitHub registry / Docker Hub to decide what should be running.
- To bring such an agent back, re-approve it via the registration UI (rebuilds/re-queues), or
  set its DB row back to `status='live'` before the deploy so `recover_live_agents` picks it up.
- The `agent-directory` GitHub registry is a **build-context store**, not the source of truth
  for what runs; deleting a stale entry there (see the delete flow) doesn't stop a running agent
  and leaving one there doesn't start it.

**`ModuleNotFoundError` or wrong Python version in agent logs**
The cluster auto-mounts its system anaconda (Python 3.7) into containers. All
entrypoints call their Python by absolute path to avoid this. If you see
this error, check that the relevant entrypoint script still uses the full path.

**vLLM returns 404 or connection errors**
Confirm the GPU job is running (`squeue -n agenticnetwork-vllm`) and that the job log shows
`vLLM discovered: http://<node>:8100/v1`. GPU jobs time out (12 h) and the node changes on each
submission, so redeploy after restarting vLLM. With `VLLM_AUTODISCOVER=0`, the `.env` value must
match the current GPU node.

**Pyxis serving a stale image after a fix**
Pyxis caches images by name and tag. Push to a new tag (e.g. `:v5`) and update
the image reference in `submit.sbatch` to force a fresh pull.

**Port conflict with another user's job**
`submit.sbatch` sets every port (base + `PORT_OFFSET`) after it reads `.env`, so `.env` port
values are ignored. Change the base port in `submit.sbatch`; no rebuild needed.

---

## Running the network from your laptop (SSH tunnel to cluster vLLM)

You don't have to run the whole stack on the cluster. A common dev loop is: **keep
heavy LLM inference on the cluster GPU (vLLM) and run everything else on your laptop**
with the local dev stack ([`docs/DEVSTACK.md`](../../DEVSTACK.md)), bridging to vLLM over
an SSH tunnel.

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
3. **Run the dev stack against the tunnel** (or put the three values in `.devstack.env`):
   ```bash
   OPENAI_BASE_URL=http://localhost:8100/v1 \
   OPENAI_API_KEY=<VLLM_API_KEY value> \
   MAIN_AGENT_LLM_MODEL=openai/gpt-oss-120b \
   scripts/stack up --llm openai
   ```
   The gated vLLM validates the key: it must match its `--api-key` (a dummy gets 401).
   Open **http://localhost:8080/app**. The planner and the seed agent both use the tunnel.
4. **(Optional) the hand-run specialists** — to run the `agent-directory` specialists against a
   real ADS daemon on `:8888` (the one vLLM tunnel serves them all), see repo-root `CLAUDE.md`
   §4 Tier B.

**The inverse** (network on the cluster, browser on your laptop) is the tunnel in
[Accessing a test instance](#accessing-a-test-instance-ssh-tunnel). A raw tunnel to prod's
planner bypasses Shibboleth, so it carries no identity and `/app` returns 401; use the public URL
for prod.

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
- **Local dev without Shibboleth:** set `PLANNER_ALLOW_ANON=1` so a request with no
  identity header falls back to `demo-user` instead of a 401 (`auth.py`); put your dev
  identity (e.g. `demo-user`) in `ADMIN_EMAILS` to get admin locally. The old
  `ADMIN_PASSWORD`/`POST /admin/login` bypass is retired (see [`docs/AUTH.md`](../../AUTH.md)).
- **Configuring the SP for the public landing:** the "Apache / Shibboleth SP" section above has
  the exact vhost carve-out that makes `/` anonymous while protecting `/app` and the APIs (P4e).

---

## Security & hazards

See [`SECURITY.md`](../../../SECURITY.md) for the current security posture, known
hazards, and the hardening checklist (no auth on specialist ports, the uploaded-code
trust boundary, `coding_agent` YOLO bash, credential handling, stored-but-not-routed).
