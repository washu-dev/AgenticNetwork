# Custom Agent Handlers — Deployment Testing Runbook

How to test the custom-handler feature end-to-end on the ENGR cluster (Tier D).
Phases 1–7 are implemented and the **local SubprocessBackend path is verified**;
the **cluster path (ClusterBackend + GitHub → Docker Hub build) has never run on
real infrastructure**. This runbook is the path to validating it.

Work top to bottom. Stop and fix at the first failing gate.

---

## Stage 0 — Code/image gaps to close BEFORE deploying

These are known gaps from implementation. The first one is **build-breaking**.

### 0.1 — Fix `Dockerfile.registration` (REQUIRED — build will fail without it)
`registration_service/requirements.txt` now ends with `-e ./agent_skeleton`, but
`Dockerfile.registration` copies `requirements.txt` and runs `pip install -r`
**before** `agent_skeleton/` exists in the image → pip can't find `./agent_skeleton`.

Edit `deployment/dockerfiles/Dockerfile.registration` to copy `agent_skeleton/`
before the install:

```dockerfile
WORKDIR /app

COPY agent_skeleton/ agent_skeleton/                     # ← ADD (before requirements install)
COPY registration_service/requirements.txt registration_service/requirements.txt
RUN pip install --no-cache-dir -r registration_service/requirements.txt
```

(`-e ./agent_skeleton` resolves against the pip CWD, which is `/app`, so
`/app/agent_skeleton` must exist. The rest of the Dockerfile is unchanged.)

> The planner image needs **no Dockerfile change** — only the rebuilt web UI.

### 0.2 — `agent_skeleton` install inside generated per-agent images — DONE (vendored)
The registration service **vendors `agent_skeleton` into each build context**: on
commit it writes `registered_agents/{slug}/agent_skeleton/` (all `*.py` +
`pyproject.toml`), and the generated Dockerfile runs `pip install ./agent_skeleton`.
No access to the private AgenticNetwork repo and no build-time secret are needed —
nothing to configure here. (`AGENT_SKELETON_PIP_SPEC` is gone.)

Implication: each agent's committed build context contains a small snapshot of
`agent_skeleton`, pinned at registration time. If you change `agent_skeleton`,
existing agents keep their snapshot until re-registered/re-approved.

### 0.3 — Spawn mechanism — DECIDED: host-side loop (Note #8)
Probed on `condo-di2` (job 92051): the registration image has **no `srun` binary**,
so the nested-srun path is out. The implemented design is the **host-side spawn
loop**: the build-poll marks agents `image_ready`, and `deployment/spawn_agent_loop.py`
(launched by `submit.sbatch`, running in the native Slurm env) does the `srun`.
Nothing to decide here anymore — just make sure the loop is wired (it is, in
`submit.sbatch`, gated on `SPAWN_BACKEND=cluster`). Stage 3 below is now a
verification of that loop, not the old nested-srun probe.

---

## Stage 1 — Secrets & configuration

There are **three** distinct secrets across **two** locations. Only two go in `.env`.

| Secret | Lives in | Type | Scope/permissions |
|---|---|---|---|
| `GITHUB_TOKEN` | `deployment/.env` | GitHub PAT | write to `agent-directory` (see 1.1) |
| `MAIN_AGENT_ADMIN_TOKEN` | `deployment/.env` | self-generated random string | n/a — shared secret (see 1.2) |
| `DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN` | **agent-directory** Actions secrets | Docker Hub PAT | push to `amaankhan25` namespace (see 1.3) |

### 1.1 — `deployment/.env` (copy from `.env.example`, fill real values)
```bash
SPAWN_BACKEND=cluster
HANDLER_FILES_DIR=/workspace/registration_service/uploaded_handlers
GITHUB_TOKEN=github_pat_xxx                 # GitHub PAT — see below
GITHUB_REPO=washu-dev/agent-directory
MAIN_AGENT_ADMIN_TOKEN=<random-32+ chars>   # self-generated; see 1.2
```
(agent_skeleton is vendored into each image build context — no pip-spec/token needed; see 0.2.)

**`GITHUB_TOKEN` — exactly what it needs.** Used server-side by the registration
service to commit each agent's build context to `agent-directory` via the Git Data
API (GET repo/ref/commit, POST trees/commits, PATCH ref).
- **Fine-grained PAT (recommended):** Resource owner `washu-dev`; Repository access
  **only `agent-directory`**; Permissions **Contents: Read and write** (+ Metadata:
  Read, auto-added). Do **not** grant "Workflows" — commits go under
  `registered_agents/`, not `.github/workflows/`.
- **Classic PAT (alternative):** scope `repo` (private) or `public_repo` (public).
- **Org gotchas (washu-dev is an org):**
  - If the org enforces **SAML SSO**, click **Configure SSO → Authorize** on the
    token or every call 403s.
  - Fine-grained PATs may need **org-owner approval** before first use.
  - The token's identity must be allowed to **push directly to the default branch**
    (`main`). The code commits straight to it — if `main` has branch protection
    requiring PRs, the push is rejected (needs a bot exception; PR-flow is not
    implemented).
- **Without `GITHUB_TOKEN` or `GITHUB_REPO`:** the build pipeline is disabled and a
  custom approval falls back to a direct spawn.

### 1.2 — `MAIN_AGENT_ADMIN_TOKEN` — a self-generated shared secret (not external)
This is **not** a third-party token — invent one: `openssl rand -hex 32`. The planner
and registration containers both read it; approve/reject/code-view calls go
planner → `/register/*` proxy → registration carrying `X-Admin-Token`. If the two
values differ, those calls get **403**. Both source the same `deployment/.env`, so
set it once. (Leave unset only for fully open local dev.)

### 1.3 — `agent-directory` GitHub Actions secrets (NOT in `.env`)
These are consumed by the build **workflow**, so they live in the
**washu-dev/agent-directory** repo → Settings → Secrets and variables → Actions:
- `DOCKERHUB_USERNAME` = `amaankhan25` (or an account with push rights to that namespace)
- `DOCKERHUB_TOKEN` = a **Docker Hub access token** (hub.docker.com → Account Settings →
  Personal access tokens) with **Read & Write** on the `amaankhan25` namespace.

> **Docker Hub image repos must be PUBLIC.** The registration service's build poll
> (`_image_exists_on_dockerhub`) queries `hub.docker.com/v2/...` **unauthenticated**.
> If `amaankhan25/agenticnetwork-custom-*` are private, the poll never sees the image
> and the agent stays `building` forever (open item #4 — no poll auth implemented).

---

## Stage 2 — Ship the code

### 2.1 — AgenticNetwork repo
```bash
git checkout -b feature/custom-agent-handler   # or your branch
git add -A && git commit && git push
```

### 2.2 — agent-directory repo (the Actions workflow)
The workflow file already exists locally at
`../agent-directory/.github/workflows/build-custom-agent.yml` (uncommitted).
```bash
cd ../agent-directory
git add .github/workflows/build-custom-agent.yml
git commit -m "Add custom-agent image build workflow" && git push
```
(`registered_agents/` is created automatically by the registration service's first
commit — you don't need to create it by hand.)

### 2.3 — Rebuild & push the changed images
```bash
cd /path/to/AgenticNetwork
bash deployment/build_and_push.sh registration   # new code + agent_skeleton + python-multipart
bash deployment/build_and_push.sh planner        # rebuilt web UI (Pending Approvals panel)
```

---

## Stage 3 — Verify the host spawn loop is running
(The old nested-`srun` probe is obsolete — confirmed the image has no `srun`, so we
use the host-side loop. See 0.3.) After Stage 4 launches the job, confirm the loop
started and can reach the registration service:
```bash
# the loop logs here:
tail -f /engrfs/project/di2/AgenticNetwork/logs/$USER/spawn_agent_loop.log
# expect: "[spawn-loop] starting reg=http://localhost:8091 poll=15s ..."

# the internal endpoints it polls should answer on the node:
curl -s localhost:8091/internal/spawn-queue   # -> [] until an agent is image_ready
curl -s localhost:8091/internal/stop-queue    # -> {"pids": []}
```
If the loop log is missing, check that `SPAWN_BACKEND=cluster` was set when
`submit.sbatch` ran (the launch is gated on it) and that `python3` is on the host PATH.

---

## Stage 4 — Launch the system & smoke-test infra
```bash
sbatch deployment/submit.sbatch
# watch logs
tail -f <logdir>/registration.log <logdir>/planner.log <logdir>/ads.log
```
Check gates:
- ADS reachable on 8888 (`dirctl` works inside the ADS container).
- Registration up: `curl -s localhost:8091/health` → `{"status":"ok"}`.
- Planner up: `curl -s localhost:8090/health`.
- Registration log shows `build_poll_loop_started`.
- Confirm env inside the registration container: `SPAWN_BACKEND=cluster`,
  `GITHUB_TOKEN` set, `HANDLER_FILES_DIR` points under `/workspace`.

---

## Stage 5 — End-to-end custom agent (the real test)

Use a **trivial** agent first (no requirements, static answer) to isolate the
pipeline from dependency/build issues.

### 5.1 — Register + upload (via the registration UI or curl)
Through the planner: open the registration UI (proxied at `/register/`), choose
**Custom (Python)**, name it `Echo Test`, pick one OASF skill, keep the default
handler template (it returns a static answer), submit. The UI does register +
upload in two steps. Confirm it lands as **Pending Approval**.

curl equivalent:
```bash
ID=$(curl -s localhost:8091/agents -H 'Content-Type: application/json' -d '{
  "name":"Echo Test","description":"echoes input","handler_type":"custom",
  "oasf_skills":[{"name":"nlp/text-summarization","id":10101}],
  "config":{"class_name":"EchoHandler","python_version":"3.11","requirements":[],"extra":{}}}' \
  | jq -r .id)
curl -s -o /dev/null -w "%{http_code}\n" localhost:8091/agents/$ID/handler \
  -F "file=@echo_handler.py;type=text/x-python"   # extends AgentHandler, returns {"answer": ...}
```

### 5.2 — Approve (planner admin UI → Pending Approvals → Approve)
Or: `curl -s -X POST localhost:8091/agents/$ID/approve -H "X-Admin-Token: $TOKEN"`

### 5.3 — Verify each stage of the pipeline
1. **GitHub commit (atomic):** `registered_agents/echo-test/` appears in agent-directory
   with `handler.py`, `Dockerfile`, `echo-test.card.json` in a **single** commit
   (message `Register custom agent: Echo Test`).
2. **Actions build:** the `Build custom agent image` workflow runs green and pushes
   `amaankhan25/agenticnetwork-custom-echo-test:latest` to Docker Hub.
3. **Status = building:** `curl -s localhost:8091/agents/$ID | jq .status` → `building`,
   and registration log shows `custom_build_started`.
4. **Poll detects image → image_ready:** within ~30s of the image appearing, the
   registration log shows `custom_image_ready ... -> image_ready` and status becomes
   `image_ready`.
5. **Host loop spawns it → live:** within one loop cycle (~15s) `spawn_agent_loop.log`
   shows `launching $ID: srun ...` and `reported pid ...`; the registration log shows
   `custom_spawned_live`; status → `live`. (Renumber the rest accordingly.)
5. **ADS:** the agent is routed (`dirctl routing list` shows it; planner `GET /agents`
   lists it).
6. **Planner calls it:** in chat, prompt the planner with a task matching the skill;
   confirm it discovers and calls the agent and returns the answer. (Or hit the
   agent's port directly with an A2A `message/send`.)

### 5.4 — Re-upload requires re-approval
Re-upload a changed handler → `approved` resets to `false` (shows Pending Approval
again). Note: the already-running instance keeps running until re-approved/redeployed
— decide whether you want approve to also restart it (current behavior does not stop
the old one on re-upload).

---

## Stage 6 — Failure-mode checks
- Upload an invalid handler (no `AgentHandler` base) → **422** with errors (UI shows them).
- Approve before uploading a handler → **409**.
- With `MAIN_AGENT_ADMIN_TOKEN` set, approve without the header → **403**; with it → 200.
- Delete the agent → unpublished from ADS, process stopped, `registered_agents/`
  left in git history (cleanup of the repo dir is manual), local handler dir removed.
- Guest mode in the planner admin panel → "Pending Approvals" nav hidden.

---

## Stage 7 — Real dependency demo (Phase 9, `refbot`)
Once the trivial agent works end-to-end, repeat 5.1–5.3 with the paper reviewer:
- Python version `3.12`, requirements `refbot>=0.1`, class `PaperReviewerHandler`.
- This additionally exercises: per-image `pip install` of a real dependency, and a
  larger handler. Watch the Actions build log for dependency resolution.
- Requires `refbot` to be installable (public PyPI or an index the build can reach).

---

## Troubleshooting quick map
| Symptom | Likely cause | Where |
|---|---|---|
| Registration image build fails on `pip install -r` | `agent_skeleton/` not COPYed | Stage 0.1 |
| Actions build fails: `agent_skeleton` not found | private AgenticNetwork repo | Stage 0.2 / `AGENT_SKELETON_PIP_SPEC` |
| Approve → 403 | admin token mismatch | Stage 1.2 |
| Actions never triggers | workflow not pushed / wrong repo | Stage 2.2 |
| Actions builds but no push | DockerHub secrets missing | Stage 1.3 |
| Stuck in `building` forever | image not pushed, or name mismatch, or DockerHub repo private (poll assumes public) | Stage 5.3 / open item #4 |
| `building` → never spawns despite image | `srun` not allowed in container | Stage 3 / Note #8 |
| Agent live but planner can't call | stored-but-not-routed in ADS | `dirctl routing list`; see root CLAUDE.md §2 |

See `docs/CUSTOM_AGENT_HANDLER_PLAN.md` → "Open Items / Follow-ups" for the full list.
```
