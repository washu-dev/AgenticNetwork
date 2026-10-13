# Onboarding: How Changes Actually Reach the Running System

Read this before adding an agent, wiring up a credential, or changing the deployed system.
It captures the handful of mental models that trip people up — the difference between *a file
in the repo* and *a live, discoverable, deployed thing*. For depth, each section links into the
reference set ([`docs/README.md`](README.md)).

---

## The four mental models

### 1. The repo is not the running system
Editing or adding files — here or in the `agent-directory` repo — changes **nothing** that's
running until two separate things happen:
1. the code is **built into a container image and deployed** (`deployment/build_and_push.sh` →
   `submit.sbatch`), and
2. for an agent, its card is **published to ADS** (below).

`git push` updates source. It does not update the images on the cluster, and it does not add
an agent to the directory. A source sync (`git pull` on the cluster) is step one of many, not
the deploy. See [`docs/deployment/engr/README.md`](deployment/engr/README.md).

Locally, `scripts/stack up` runs your working tree directly ([`docs/DEVSTACK.md`](DEVSTACK.md));
use it, and CI, before touching a deployment.

### 2. An agent is discoverable only when it's *published to ADS* — not when its card file exists
The planner has **zero hardcoded agent list**; it discovers agents at runtime by querying ADS's
**routing index**. A `*.card.json` sitting in a repo is inert. For the planner to see an agent,
that agent must be **running and published** (`dirctl push` + `routing publish`, then confirmed
routed). Dropping a card or an agent folder into `agent-directory/` does **not** register,
publish, or deploy it — nothing will find it.

There's also a silent failure mode: a record can be **stored but not routed** (pushed but not in
the routing index) → invisible to discovery with no error. Always confirm it routed.
See [`docs/AGENT_DIRECTORY.md`](AGENT_DIRECTORY.md) and [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

### 3. Credentials are per-user by default; env-vars are only for shared operator infra
We deliberately moved user-facing credentials to a **per-user model** (D5/D6 in
[`docs/DECISIONS.md`](DECISIONS.md)). The rule is *who owns the resource*:
- **User-owned** (a user's Google account, their OpenAI/GitHub key): the user adds it in the
  planner's **Settings → Credentials**, grants it to the agent, and the planner injects it —
  scoped, per-request — into the agent's task context. The agent **reads it from `context`**, not
  from an env var. Do **not** hardcode a user's key as an env var.
- **Operator-owned** (a shared service/infra key the org pays for — Brave, Maps, vLLM): read from
  an env var at call time, never stored by us. This is the endpoint-wrapper's `auth_env`.

If you find yourself adding `os.getenv("SOME_USER_API_KEY")` to a user-facing agent, that's the
wrong model — see [`docs/AUTH.md`](AUTH.md) §4.

### 4. Auth is planner-only, behind Shibboleth
Only the planner (`:8090`) is authenticated (WashU SSO via an Apache/Shibboleth gateway that
lives *outside* this repo). Admin = your SSO email in `ADMIN_EMAILS` (no shared token). Specialist
ports have no auth and are safe only on the trusted node. See [`docs/AUTH.md`](AUTH.md) and
[`SECURITY.md`](../SECURITY.md).

---

## "I want to…" — the correct path

### …add a new agent
There are two lanes; pick by what you're building.

**A. A user-contributed / self-service agent → use the registration service.** This is the
managed path: it builds, spawns, health-checks, and **publishes to ADS for you**, and (for
uploaded code) routes it through admin approval.
- In the planner UI: `/register/` (or `:8091` directly). Choose the handler type —
  three of them, split on *who runs the loop*:
  - `llm` — we run the loop. A **Tools** field picks where its capabilities come from:
    none (prompt only), preset catalog tools, **uploaded tool code**, or typed HTTP
    operations over an external API. Auto-published on register *except* for uploaded
    tool code, which is admin-approved like `custom`.
  - `proxy` — no LLM; forwards each call to an upstream HTTP service you host;
    auto-published.
  - `custom` — upload a `.py` or a multi-file `.zip` defining an `AgentHandler`; an admin
    reviews + **approves**, then it spawns and publishes.
  (The older `api_proxy` / `tool_loop` / `endpoint_ops` names still work on the API and
  map onto the three above.)
- You do **not** touch ADS or `dirctl` manually. See [`docs/REGISTRATION_SERVICE.md`](REGISTRATION_SERVICE.md)
  and [`docs/BUILDING_AGENTS.md`](BUILDING_AGENTS.md).

**B. A first-class specialist agent → it lives in `agent-directory`, is deployed, and
self-publishes.** Adding the folder is only the first step. The full path:
1. Add the agent code + `*.card.json` + `Dockerfile` + `entrypoint.sh` in `agent-directory`.
2. Build + push its image; add an `srun` step in `deployment/submit.sbatch`.
3. The agent **publishes its own card on startup** (copy a `publish_*_to_ads` helper) and
   unpublishes on shutdown — and polls routing to confirm it's discoverable.
   
Adding the folder without steps 2–3 means it never runs and never publishes → the planner can't
see it.

### …give an agent access to a credential
- **User-owned:** declare the credential on the agent (registration UI / card
  `capabilities.extensions`); the user adds it in Settings and grants it; read it from
  `context` in your handler. [`docs/AUTH.md`](AUTH.md) §4, [`docs/BUILDING_AGENTS.md`](BUILDING_AGENTS.md).
- **Operator-owned shared key:** add it to `deployment/.env` and read it via env var (only for
  genuinely shared infra, not per-user resources).

### …change the deployed system
Editing source is not deploying. To ship a change: `deployment/build_and_push.sh <service>` to
rebuild the image (bump the tag if reusing `:latest`), then resubmit `submit.sbatch`. Config-only
changes go in `deployment/.env`. See [`docs/deployment/engr/README.md`](deployment/engr/README.md).
On GCP, see [`docs/deployment/gcp/README.md`](deployment/gcp/README.md) "Dev workflow".

---

## Common pitfalls (and how to catch them)

| Symptom | Cause | Fix |
|---|---|---|
| "I added my agent to `agent-directory` but the planner can't see it." | A card file was added, but the agent was never deployed/run or never published to ADS. | Register it via the registration service (A), or deploy it + self-publish (B). Confirm with `run_ads.sh -list`. |
| "The agent is registered but still not discoverable." | Stored-but-not-routed (pushed, not in the routing index). | `run_ads.sh -diag` to classify it; `-repair` to route it. |
| "My agent reads the key from an env var but each user should use their own." | Wrong credential model (operator env-var used for a user-owned resource). | Declare it, grant it in Settings, read from `context`. [`docs/AUTH.md`](AUTH.md) §4. |
| "I pushed to `main` / synced the cluster but nothing changed." | Source sync ≠ redeploy; the running system uses built images. | Rebuild + push images, resubmit `submit.sbatch`. |
| "Admin routes 403 for everyone." | `ADMIN_EMAILS` unset (fail-closed). | Set `ADMIN_EMAILS` in `deployment/.env`. |

## Verify your work
```bash
# Locally: is the dev stack ready, and does one prompt reach a specialist?
scripts/stack status --json
scripts/stack smoke

# Is my agent actually discoverable (routed) in ADS?
agent_directory_service/run_ads.sh -list          # routed agents
agent_directory_service/run_ads.sh -diag          # healthy / queued / stale / orphaned

# Does the planner see it?
curl -s http://localhost:8090/agents | jq '.agents[].name'   # from the compute node

# Test an agent's A2A endpoint directly, without publishing:
python -m agent_directory_service.scripts.validate_agent_endpoint \
  --card <card.json> --skill <skill> --prompt "..." --endpoint http://host:port/
```

## Where to go deeper
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md) · [`docs/REGISTRATION_SERVICE.md`](REGISTRATION_SERVICE.md) ·
[`docs/AGENT_DIRECTORY.md`](AGENT_DIRECTORY.md) · [`docs/BUILDING_AGENTS.md`](BUILDING_AGENTS.md) ·
[`docs/AUTH.md`](AUTH.md) · [`docs/DECISIONS.md`](DECISIONS.md) · [`docs/deployment/engr/README.md`](deployment/engr/README.md)
