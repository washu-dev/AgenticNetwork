# GCP Migration Plan — On-Demand Agent Network

> **Status:** Draft v1 (2026-07-13) — initial plan, iterate freely.
> **Goal:** Move the AgenticNetwork deployment off the WashU ENGR Slurm cluster and
> onto GCP, re-architected around **on-demand / scale-to-zero agent instances**
> (the $5,000-GCP-credit scalability prototype). This is *not* a lift-and-shift —
> the always-on `srun --overlap` model is deliberately replaced.
>
> **Milestone 1 (agreed by the team, 2026-07-08):** one right-sized GCP instance
> **per user query** — spin up on demand, run the agent for that session, tear it
> down. Multi-instance concurrency / scheduling is explicitly deferred.
>
> **Guiding principle — additive, not a replacement (decided 2026-07-13):** the
> Slurm/ENGR deployment (`submit.sbatch`, `start_vllm.sbatch`, `dockerfiles/`,
> `entrypoints/`, `.env`) **stays as a maintained, working fallback** for the whole
> prototype. Nothing here deletes or breaks the cluster path. GCP is built out
> **in parallel** (new files, new backend, new config), and we only *consider*
> cutover once the cloud path is proven — see Phase 6. Where code is shared (planner,
> registration service, cards), changes must keep Slurm working (env-flag or
> backend-selector gated, mirroring the existing `SPAWN_BACKEND=cluster` pattern).
>
> Companion docs: `DOWNSTREAM_CONSIDERATIONS.md` (#1 scale-to-zero, #2 per-agent,
> #3 cross-cutting), `AGENT_INTEGRATION_SPEC.md` (dependency classes), `README.md`
> (current Slurm runbook), `../CLAUDE.md`.

---

## 0. Two scaling goals — and what milestone 1 targets (reconciled 2026-07-10)

Two team meetings pushed at **different** scaling problems; the plan sequences them so
we don't conflate them:

| | **7-08 (Roch): on-demand agents** | **7-10 (Dan/advisor): control-plane serverless** |
|---|---|---|
| Scale *what* | **Agents** — right-sized instance per query, torn down | **Planner + registry** — auto-scale for concurrent *users* |
| Driver | Idle-cost of always-on agents ($5k credit prototype) | Hackathon: handle ~50 concurrent users; load testing |
| Mechanism | Agents on an on-demand tier (GKE) | **Cloud Run** for the stateless control plane |

Dan was explicit (7-10): **serverless applies to the *infrastructure layer* (planner,
registry), NOT the agents.** So "serverless" and "on-demand agents" are complementary,
not the same thing. Sequencing:

- **Phase 1 (immediate — serves the Monday design review + the hackathon):** migrate the
  **control plane** to GCP serverless + Cloud SQL + Vertex, with WashU-IP-restricted
  networking, then **load-test**. Agents come along as-is (still container-per-agent;
  not yet per-query). This is the concrete near-term deliverable.
- **Phase 2 (the $5k scalability prototype):** on-demand per-query agent spin-up/teardown
  (§3). Bigger lift; builds on Phase 1's substrate.

Everything below still describes the full target (both phases); §8 gives the build order.

---

## 1. Where we are today (the thing we're replacing)

The current deployment (`submit.sbatch`) is a **single Slurm CPU node running 10
containers as parallel `srun --overlap` steps**, all talking over `localhost`:

```
Slurm CPU job (one node, 4h allocation)          Slurm GPU job (separate)
┌──────────────────────────────────────┐        ┌───────────────────────┐
│ postgres:5432  ads:8888  registration │        │ vLLM  gpt-oss-120b     │
│ planner:8090   + 6 specialists        │◀──────▶│ :8100  (1× H100)       │
│ (email/scout/coding/intel/disaster/geo)│  http  └───────────────────────┘
└──────────────────────────────────────┘         hostname pasted into .env
        │                                          each run (ephemeral)
        ▼ localhost:PORT everywhere
```

Load-bearing properties that **do not survive** an on-demand cloud move:

| Property today | Why it breaks on-demand/cloud |
|---|---|
| All agents always-on for the full allocation | The exact thing we're removing — pay only when called |
| Agents self-publish their card to ADS on boot, unpublish via `atexit` | **The dominant blocker.** A stopped agent disappears from the directory → planner never discovers it → never spins it up (chicken-and-egg, `DOWNSTREAM #1`) |
| Everything reachable via `localhost:PORT` on one node | No shared localhost across cloud instances; need stable service DNS + real networking |
| Card `url`s are `127.0.0.1:<port>` / LAN IPs | Planner dials these; must become stable per-agent DNS |
| vLLM endpoint = ephemeral Slurm hostname re-pasted into `.env` | No stable inference endpoint |
| Persistent state on `/engrfs` bind-mounts (`postgres_data`, logs) | No shared HPC filesystem on GCP → Cloud SQL + cloud logging |
| Secrets in `.env` (Slurm-propagated) + baked creds (email Google creds `COPY`'d) | Multi-tenant cloud needs Secret Manager / Workload Identity |
| `srun` is the container runtime (Pyxis/enroot); host spawn-loop bridges custom agents because the registration container can't call `srun` | No Slurm → the spawn mechanism is rewritten entirely |
| Auth = Apache+`mod_shib`+nginx gateway **outside the repo**, trusts injected `eppn`/`mail` headers | Gateway must be rebuilt with GCP-native identity (or the header contract re-fronted) |
| Two directories (`dirctl` :8888, scout gRPC :8889); 5+ duplicate ADS-addr env vars | Needs unification for a clean cloud directory |

What **does** carry over cleanly: the A2A contract (HTTP/JSON-RPC), the OASF/`dir`
card model, the planner's agent-agnostic 5-tool design, and — critically — the
`registration_service` `ContainerBackend` abstraction (`SubprocessBackend` /
`ClusterBackend`), which was designed to accept a new cloud backend.

---

## 2. Target architecture on GCP

A **hybrid substrate** (per your steer: serverless for the stateless pieces, a
separate mechanism for on-demand agent containers):

```
                    ┌─────────────────────── GCP project ───────────────────────┐
  Browser           │                                                            │
    │  HTTPS         │   ── CONTROL PLANE (serverless, ~always-on but cheap) ──   │
    ▼                │   ┌────────────┐  ┌──────────────┐  ┌──────────────────┐  │
 Cloud LB + IAP  ───────▶│  Planner   │  │ Registration │  │  ADS (directory) │  │
 (identity)         │   │ Cloud Run  │  │  Cloud Run   │  │  Cloud Run/GKE   │  │
                    │   └─────┬──────┘  └──────┬───────┘  └────────┬─────────┘  │
                    │         │  A2A            │ deploy/delete     │ pre-publish │
                    │         │                 ▼                   │ catalog     │
                    │         │        ┌───────────────── ON-DEMAND AGENT TIER ──┤
                    │         └───────▶│  GKE Autopilot (the "something else")    │
                    │  activate + wait │  per-query Pod/Job, right-sized from     │
                    │                  │  the card's resource field, torn down    │
                    │                  │  after the session. GPU node pool for    │
                    │                  │  agents that need it.                    │
                    │                  └──────────────────────────────────────────┤
                    │   ── SHARED DATA/PLATFORM ──                                 │
                    │   Cloud SQL (Postgres)   Secret Manager   Artifact Registry  │
                    │   Cloud Logging/Monitoring                                   │
                    │   ── LLM TIER (see §4 — decision pending) ──                 │
                    └────────────────────────────────────────────────────────────┘
```

### 2.1 Component-by-component target

| Component | Today | GCP target | Rationale |
|---|---|---|---|
| **Planner** | `srun` container, always-on, `dirctl` subprocess | **Cloud Run** service, stateless, min-instances 0–1 | Already Chat-Completions-ready + stateless per-turn; natural serverless front door. Must drop the local `dirctl` subprocess for a network directory client. |
| **Registration svc** | `srun` container + host spawn-loop | **Cloud Run** service + new **on-demand backend** (deploys to GKE/Cloud Run) | The `ContainerBackend` interface already anticipates this; replace `ClusterBackend`'s host-`srun`-loop with a GCP API client. |
| **ADS (directory)** | `dirctl daemon`, ephemeral `dir` DB, in-container OCI store + libp2p routing | **Cloud Run (min-instances=1)** or a small **GKE Deployment** | libp2p gossip + the `localhost:5000` OCI registry ref make it *stateful-ish*; needs a persistent-enough home. **Open question §7.** |
| **Postgres** | container on `/engrfs` bind-mount | **Cloud SQL for PostgreSQL** | Managed, durable, replaces the shared-FS assumption. `registration` DB → persistent instance; `dir` DB → same instance, reset per deploy (or its own ephemeral schema). |
| **Specialist agents (6)** | hardcoded `srun` steps, always-on | **Cloud Run service per agent**, `min=0` (scale-to-zero per query), right-sized from the card | The core of the prototype. Card presence decoupled from process (see §3). GKE only for a future GPU/isolation-demanding agent. |
| **Custom agents** | GitHub→DockerHub build → `image_ready` → host spawn-loop `srun` | Same build pipeline → **Artifact Registry** → deploy a `min=0` Cloud Run service | Reuse the existing register→approve→build lane; only the *launch* mechanism changes. |
| **LLM (vLLM)** | separate Slurm GPU job, hostname pasted into `.env` | **§4 decision:** managed (Vertex/Gemini) *or* self-hosted vLLM on Cloud Run GPU / GKE GPU | Answers your open question. |
| **Image registry** | Docker Hub `amaankhan25/*` (tied to one personal account — a known team blocker) | **Artifact Registry** + GitHub Actions CI/CD | Decouples builds from a personal Docker Hub account (team ask, 2026-07-08). |
| **Secrets** | `.env` + baked creds | **Secret Manager** + Workload Identity | No baked/env secrets in a multi-tenant cloud. |
| **Gateway / auth** | nginx + Apache + Shibboleth (outside repo) | **Cloud Load Balancing + IAP**, or keep the header-injection contract behind a GCP proxy | Planner trusts injected identity headers; §5 covers options. |

### 2.2 On-demand agent tier — DECIDED (2026-07-13): Cloud Run per agent

> **Decision:** each on-demand agent is a **Cloud Run service with `min=0`** (scale-to-zero
> per query). Cloud Run provides activation, teardown, a stable URL, and crash-recovery for
> free; **no current agent needs a GPU** once LLM is on Vertex; and it's the lowest-ops path
> for the ~4-week window. **GKE is the graduation path** — adopt it for the first agent that
> declares a GPU/TPU need or requires strict network-egress isolation. The discussion below
> (originally leaning GKE) is retained as the rationale; the Vertex decision is what flipped
> it toward Cloud Run.

The earlier framing was "serverless for the stateless pieces, **something else** for the
on-demand containers" → **GKE Autopilot**, for these reasons (still valid *if/when* a GPU
agent appears):

- **Per-query, right-sized instances** map cleanly to K8s Pods/Jobs whose CPU/mem/GPU
  come straight from the card's new resource field (§3.3). Autopilot **node
  auto-provisioning** boots a right-sized (incl. GPU) node on demand and removes it
  after — this is Roch's "spin up a right-sized instance, tear it down" almost verbatim.
- Matches the `KubernetesBackend` the original registration-service plan already named.
- GPU node pools live in the same cluster as CPU agents — one substrate for both.

**Worth flagging (a genuine tradeoff for us to decide):** *Cloud Run per agent* would
give scale-to-zero **for free** — each agent = one Cloud Run service with
`min-instances=0` and a **stable HTTPS URL** (which doubles as the card `url`, solving
`DOWNSTREAM #3`'s DNS requirement). Cloud Run's built-in cold-start-on-request **is**
the "activation proxy" that `DOWNSTREAM #1` says we'd otherwise have to build. The cost:
less control over exact instance shape, per-request VM-level isolation is weaker than a
fresh GKE Job (matters for `coding_agent`'s YOLO bash — see §6), and it's "serverless"
which you steered away from for this tier. **I lean GKE for milestone 1** to match the
"one instance per query" framing and the isolation needs, but Cloud Run per agent is the
lower-ops path and we should consciously pick. (See §7 open questions.)

### 2.3 Serverless (Cloud Run): how it works and how much applies

**What it is.** You hand Cloud Run a container image; it runs it and manages *when and
how many* copies exist — no VMs to operate. This is the "serverless" Dan recommended for
the infrastructure layer (7-10). **Cloud Run, not Cloud Functions:** everything here is
already containerized, so Cloud Run runs the images as-is; Functions would mean rewriting
services for no benefit.

**The mechanics (and the four properties that decide fit):**
- **Request-driven autoscaling.** Each instance serves up to *N* concurrent requests
  (default 80); load beyond that spins up more instances, up to a configured max. This is
  the horizontal scaling that answers "can we handle 50 concurrent users?"
- **Scale-to-zero + cold starts.** `min-instances=0` → pay nothing when idle, but the
  first request after idle pays a **cold start** (image pull + boot). Set `min-instances=1`
  to keep one warm for demos (small cost, no cold start).
- **Request timeout = 60 min max** (default 5). Long LLM calls, streaming/SSE, and the
  coding agent's ~25s heartbeat all fit — Dan's "long-running process" worry is **not** a
  blocker.
- **Statelessness (the real constraint).** Instances are ephemeral and interchangeable;
  in-memory/local-disk state can vanish and the next request may hit a different instance.
  All durable state must be external — **already true here** (chat history, pending
  confirmations, credentials, agent metadata are in Postgres; planner per-turn state is
  request-scoped).
- **Background work is throttled.** Cloud Run gives CPU mainly *during* a request; a
  continuous background loop needs `min-instances≥1` **and** "CPU always allocated." This
  is the catch for the registration service's supervisor (below).

**How much applies — per component:**

| Component | Serverless fit | Notes |
|---|---|---|
| **Planner** | ✅ Ideal | Stateless, request/response — the real scaling win; auto-scales for concurrent users. |
| **Registration svc** | ⚠️ Partial | REST API is stateless, but its **singleton supervisor** (health loop, crash-recovery, spawns) is a background loop that must be a *single* instance. Run as `min=max=1` + CPU-always, or split API (scalable) from worker (singleton). Scaling it to N would create N competing supervisors. |
| **ADS / directory** | ❌ Poor | Stateful long-lived daemon (libp2p routing, node identity key, OCI store) → wants always-on, not scale-to-zero. Run as a single always-on service (Cloud Run min=1 CPU-always, or small GKE/VM). |
| **Postgres** | ❌ (by design) | Managed persistent service = **Cloud SQL**, not Cloud Run. |
| **Agents** | ➖ separate tier | Not the serverless infra layer (Dan). CPU agents *could* be scale-to-zero Cloud Run services each; GPU agents can't cleanly → the on-demand/GKE Phase-2 question. |

**Takeaway:** serverless is a *textbook* fit for the **planner**, a *qualified* fit for the
**registration service** (singleton-supervisor caveat), and **not** a fit for ADS or
Postgres. That's the precise meaning of Dan's "infra layer, not agents."

### 2.4 Registration-service supervisor split — DECISION (2026-07-13)

**Why it's not stateless (from the code).** `Supervisor` (`registration_service/supervisor.py`)
is a module-level singleton that the FastAPI handlers drive *and* that runs two background
loops (`_health_loop`, `_build_poll_loop`, started in `lifespan.py`). It also holds
**in-memory cross-request state**: `SubprocessBackend._procs` (owns child processes as OS
children) and `ClusterBackend._procs` + `_stop_pids` (the stop queue). A `DELETE /agents/{id}`
appends a PID to in-memory `_stop_pids`; `GET /internal/stop-queue` reads it back — if those
hit different instances, the delete is lost. N replicas → N health loops → restart storm. So
it **cannot** run as an elastic multi-instance Cloud Run service.

**Key realization:** most of that statefulness is a **Slurm artifact**. The in-memory PID
tracking, `_stop_pids`, and the whole `/internal/spawn-queue` + `spawn_agent_loop.py`
host-loop bridge exist *only* because the registration container can't call `srun`. On GCP
the on-demand backend calls the orchestration API directly, and that API is the source of
truth: `is_alive` = query GKE/Cloud Run API; `stop` = call delete API; crash-restart =
Kubernetes liveness probes + `restartPolicy`. The cloud backend is *simpler* than
`ClusterBackend`; the only genuinely singleton work left is the build-poll loop + ADS
reconciliation.

**Decision:**
- **Phase 1:** run the registration service as **one pinned Cloud Run instance** —
  `min=max=1`, **CPU always allocated**. Do **not** split. It's low-QPS management traffic
  (register/approve/delete), *not* the user-query hot path (the **planner** is what scales).
  Pinning to 1 keeps the singleton loops correct with near-zero code change.
- **Phase 2 (updated 2026-07-13 — agents are Cloud Run, not GKE):** it **stays a pinned
  Cloud Run singleton** and spawns/stops agents via the **Cloud Run Admin API** (callable
  from the container — unlike `srun`). This still **deletes** the host-spawn-loop, the
  `/internal/*` bridge, and the in-memory PID/stop-queue state; Cloud Run provides agent
  crash-recovery. (Only migrate the registration service itself to GKE if the agent tier
  later moves to GKE for GPU/isolation reasons.)
- **Deferred:** the API/worker split (scalable stateless API + singleton worker via a
  Postgres work-queue) — only if load testing later shows the registration *API itself* is a
  bottleneck (unlikely, off the hot path).

**Through-line:** the registration service never needs to scale horizontally — it needs to be
a correct **singleton**. "Pin it to 1" (Cloud Run now → GKE `replicas=1` later) is the answer,
and the GCP move *removes* most of its accidental complexity.

---

## 3. The three code changes that make on-demand possible

These are the load-bearing engineering items. Everything else is plumbing.

### 3.1 Decouple ADS publishing from agent process lifetime  *(DOWNSTREAM #1)*
**Problem:** agents publish their card on boot and unpublish on exit, so a
scaled-to-zero agent is invisible and can never be chosen.
**Fix:** the directory must advertise the **full catalog** independent of what's
running. The `registration_service` already does this for its managed agents
(`Supervisor.spawn_and_publish` / persistent Postgres `agents` table) — extend that
so **every** agent (incl. the 6 specialists) has its card pre-published and kept
**routed** (not just stored — the stored-vs-routed trap). Remove the specialists'
boot-time self-publish / `atexit`-unpublish. Enabler in repo:
`agent_directory_service/scripts/publish_agents.py` publishes a card with no agent
running.

### 3.2 An on-demand backend + planner activation step  *(DOWNSTREAM #1)*
**New backend** implementing the existing `ContainerBackend` interface
(`registration_service/backend.py`), e.g. `GKEBackend` (or `CloudRunBackend`):
- `spawn(agent_id, …, resources)` → create a right-sized Pod/Job (or Cloud Run
  service), return once `/health` is green.
- `stop(agent_id)` → delete the workload.
- `is_alive(agent_id)` → check readiness.

**Planner dispatch change:** `main_agent/runtime.py::_tool_call_agent` currently
assumes the endpoint is live. Add an **activate-and-wait** step before the A2A call:
resolve the chosen agent → ask the activation layer to bring it up → poll health →
then dispatch. Cold-start latency now sits in the live request path — mitigate with
image pre-pull / warm pool for hot agents (§6).

**Teardown policy for milestone 1:** simplest is *per session* — tear down after the
query/session completes or after an idle timeout. (True per-*query* teardown is fine
too; decide based on how chatty a session is — §7.)

### 3.3 Card resource-requirements field  *(team decision, 2026-07-08)*
Add a **developer-declared** (not LLM-inferred) resource block to the agent card —
CPU / memory / GPU (and GPU type). Mirror the existing extension-field pattern used
for P4 credentials (`agenticnetwork.dev/ext/required-credentials`), e.g.
`agenticnetwork.dev/ext/resource-requirements`. The on-demand backend reads it to
size the Pod/Job. Default sensible values when absent. This is a small schema +
converter + validator change (registration service already validates cards).

---

## 4. LLM hosting on GCP — options (answering your open question)

vLLM currently serves `gpt-oss-120b` and exposes an **OpenAI-compatible Chat
Completions** API, which the planner + most agents already speak. On GCP you have
three real paths; they trade GPU-ops for model choice and cost. (All should be
**verified against current GCP quota/availability** before committing — cloud
features move fast.)

| Option | What it is | OpenAI-compatible? | GPU ops | Cost shape | Credits apply? |
|---|---|---|---|---|---|
| **A. Managed — Gemini via Vertex AI** ⭐ | Google's hosted Gemini through **Vertex AI's** OpenAI-compat Chat Completions endpoint | Yes (native compat layer) | **None** | **Pay-per-token** (no idle cost) | **Yes — via Vertex** (see below) |
| **B. Self-host vLLM on Cloud Run GPU** | vLLM packaged as a Cloud Run service with an L4 GPU, `min-instances=0` | Yes (it's vLLM) | Low–medium | GPU-hour while warm; $0 idle (scale-to-zero) | Yes (GPU credits) |
| **C. Self-host vLLM on GKE GPU node pool** | vLLM Deployment on a GPU node pool, scale 0↔N | Yes | Highest | GPU-hour per running node | Yes (GPU credits) |

**How managed plays with the $5k credits (your question).** Confirmed from GCP docs:
- **Credits apply to Vertex AI**, including Gemini accessed *through Vertex*. The catch:
  you must route through **Vertex AI**, **not** the "direct" Gemini API in Google AI
  Studio — that path is a separate billing track that (for accounts created after
  2026-03-02) credits can't be spent on. **Same models, different door; the Vertex door
  draws down Cloud credits.** So: use Vertex, not an AI-Studio API key.
- **Cost shape favors a bursty prototype.** Managed Gemini is **pay-per-token** — you
  pay only for the queries you actually run, with **zero idle cost**. A demo that runs
  a few dozen queries a day costs cents. A self-hosted GPU (B/C) costs GPU-hours
  *whenever it's warm*, even between demos (scale-to-zero helps, but cold starts then
  reload the model). For $5k stretched across a prototype window, **managed is the most
  credit-efficient** unless "offload the ENGR H100" is itself the goal.

**Integration notes for Option A (so we scope it right):**
- **Drop-in endpoint.** Point the OpenAI SDK's `base_url` at
  `https://{region}-aiplatform.googleapis.com/v1/projects/{proj}/locations/{region}/endpoints/openapi`
  (Chat Completions). The planner (`llm_client.py`) is already Chat-shaped, so this is a
  config swap, not a rewrite.
- **Model + region — VALIDATED 2026-07-14 (laptop *and* an ENGR compute node).** Confirmed
  working end-to-end against project `di2-agenticweb`: **`us-central1` + model
  `google/gemini-2.5-flash`**, token via `gcloud auth print-access-token`. Egress from an
  ENGR node to `*.googleapis.com:443` is open, so the LLM leg is reachable from where the code
  runs today. Two constraints this surfaced:
    - **`gemini-2.0-flash-001` was discontinued 2026-06-01** — do not use the old "2.0 Flash"
      name; `gemini-2.5-flash` is the current regional Flash.
    - **Gemini-3.x (e.g. `gemini-3.5-flash`) is served only from the `global` endpoint** —
      regional `us-central1` returns 404, and the `global` host drops the region prefix
      (`aiplatform.googleapis.com`, *not* `global-aiplatform...`). **This is a real tension with
      the single-region-for-egress decision:** staying regional caps us at ≤ Gemini 2.5;
      adopting 3.x means routing through `global` (Google picks the serving region), which
      reopens the egress/data-residency question. Decide consciously per model generation.
- **Auth wrinkle.** Unlike vLLM's static dummy key, Vertex's `api_key` is a **short-lived
  GCP access token** (from a service account / ADC, ~1h TTL). We add a small token-refresh
  helper (google-auth) and relax the agents' hard `OPENAI_API_KEY` guards. One shared
  concern across planner + litellm agents (coding/intelligence honor `OPENAI_API_BASE`).
- **Multimodal is supported.** Vertex's Chat Completions accepts base64 image parts, so
  **geospatial's vision path can work** — a major simplification vs. self-hosting text-only
  `gpt-oss-120b` (which would lose vision).

**Decision (2026-07-13): Option A — managed Gemini via Vertex AI — is the primary GCP
LLM path.** Rationale: lowest ops, credit-eligible, pay-per-use, and it *unblocks* the
two hardest per-agent rewrites (`DOWNSTREAM #2`) — email + geospatial still need the
Responses→Chat rewrite against any OpenAI-compat endpoint, but Gemini's native vision +
grounding means geospatial doesn't *also* need a separate GPU vision tier.

**Backup (standing, not going away): the ENGR H100 + `gpt-oss-120b` vLLM job.** Per the
guiding principle, `start_vllm.sbatch` stays maintained. The planner already selects its
LLM endpoint by env (`OPENAI_BASE_URL` / model name), so switching between "Vertex" and
"ENGR vLLM" is a config change, not a code change — the backup is a base-URL flip. Option
C (self-host vLLM on GCP GPUs) is **not** pursued now; if the team later wants GPU on GCP,
Option B (Cloud Run GPU vLLM) is the documented path.

---

## 5. Identity, gateway & auth on GCP

"Auth" is overloaded here — it means **three independent things**, solved in three
different places. Keeping them separate avoids confusion:

| Layer | Question it answers | Where it lives | Status today |
|---|---|---|---|
| **1. User login** | "Is this human a real WashU person, and who?" | A gateway *in front of* the planner | WashU Shibboleth (working) |
| **2. Service-to-service (A2A)** | "May this planner call this agent?" | Planner ↔ agents | **None** — agents bind `0.0.0.0`, empty `securitySchemes` |
| **3. Cloud / LLM auth** | "May this app call Vertex/GCP?" | Planner → Google APIs | New — arrives with the Vertex decision (§4) |

### Layer 1 — User login (the WashU SSO gateway)

**How it works today (the key insight):** the planner has *no login code of its own*.
`agenticweb.engr.wustl.edu` → nginx (TLS) → Apache + `mod_shib` (WashU SAML SSO) →
injects `eppn`/`mail`/`remote-user` headers → **the planner blindly trusts those
headers** (`main_agent/auth.py`). All real SSO work is done by the proxy. Two
consequences drive the design:
1. **The planner must never be reachable directly** — anyone who can hit it bypassing the
   proxy can just send a fake `eppn` header and impersonate any user. Locking the back
   door is the security-critical requirement in *any* option.
2. A "gateway" is simply any box that (a) forces WashU login and (b) injects a trusted
   identity header. The options below are two such boxes.

**Constraint (decided 2026-07-13): login must stay WashU SSO** — rules out plain
GCP-native Google login.

**DECISION (2026-07-13): Option 1 now; Option 2 as a planned future hardening step.**

- **Option 1 — Keep the WashU Shibboleth proxy, point it at GCP (CHOSEN for milestone 1).**
  Reuse the *exact same* Apache+`mod_shib` host; the only change is its upstream target →
  the planner's **stable** Cloud Run URL instead of an ephemeral Slurm node.
  - *Fixes for free:* eliminates the "repoint the upstream after every Slurm run" pain
    (`DOWNSTREAM #3`) — a Cloud Run URL is permanent, so you repoint once.
  - *No code change:* `auth.py` and the WashU-IT Shibboleth SP registration are untouched.
  - *Must-do (back-door lock):* set the planner's Cloud Run **ingress to "internal + load
    balancer" only** so the public internet cannot reach it directly — only the proxy can.
    Without this, header-trust is wide open.
  - *Cost:* keep running the one bespoke Apache/Shibboleth host (already WashU-IT-owned).

- **Option 2 — Workforce Identity Federation + IAP (PLANNED FUTURE — do eventually).**
  Replace the self-managed Apache/Shibboleth box with Google-managed pieces: **IAP**
  (Identity-Aware Proxy — the managed front door that forces login and passes a signed
  `X-Goog-Authenticated-User-Email` header) in front of Cloud Run/GKE, plus **Workforce
  Identity Federation** to federate WashU's SAML IdP so users still log in with WashU
  credentials.
  - *Why eventually:* removes the bespoke Apache host entirely; Google runs/scales/secures
    the gateway → less long-term ops.
  - *Cost to adopt:* larger one-time setup, requires **WashU IT coordination** to register
    the SAML federation, and a small `auth.py` change to read the IAP header instead of
    `eppn`/`mail`.
  - *Trigger to revisit:* when we want to retire the out-of-repo Apache box, or when
    onboarding it becomes a maintenance burden.

### Layer 2 — Service-to-service (planner → agents)

Today there is none. For milestone 1 the simplest safe answer is **network-level
isolation**: keep agents on an internal VPC / internal-only ingress so nothing public can
reach them — only the planner can. This matters more once agents are spun up on demand
(they must not be publicly callable). Signed A2A tokens / populated `securitySchemes`
(`DOWNSTREAM #3`) are a later hardening step.

### Layer 3 — Cloud / LLM auth (planner → Vertex)

Comes with the Vertex decision (§4). The planner authenticates to Google with a **service
account via Workload Identity** — on Cloud Run/GKE the platform hands the app short-lived
credentials automatically, and the OpenAI SDK sends a ~1h token as its `api_key`,
auto-refreshed. **No static API key to store or rotate.** Entirely separate from user
login (Layer 1): this is the *app* proving itself to GCP, not a *human* proving they're
WashU.

---

## 6. Cross-cutting hardening (mostly `DOWNSTREAM #2/#3`)

- **Secrets → Secret Manager.** `CREDENTIALS_ENCRYPTION_KEY`, `GITHUB_TOKEN`, Google
  OAuth client, and the AES-encrypted per-user credential DB all move to Secret
  Manager + Workload Identity. Remove baked email Google creds from the image
  (`Dockerfile.email COPY deployment/google_credentials/`). Externalize email's
  on-disk OAuth **refresh tokens** to the shared DB so on-demand restarts don't lose
  auth.
- **Directory client, not `dirctl` subprocess.** Planner (and coding/intelligence
  `_vendor_v2/` copies) shell out to the `dirctl` binary. Replace with a network/gRPC
  directory client so a serverless pod needs no binary + no local daemon.
- **Unify the two directories.** Reconcile scout's gRPC `AgentDirectory` (:8889) with
  `dirctl` (:8888) onto one backend; collapse the 5+ duplicate ADS-address env vars.
- **Stable service identities.** Replace the 5-digit port remaps (19105–19108) and the
  advertise-url≠bind-port inconsistencies with per-agent DNS/service names.
- **`coding_agent` isolation.** It runs mini-swe-agent in **YOLO bash mode**. In a
  multi-tenant cloud each request needs strong isolation — a fresh per-request GKE Pod
  (gVisor/sandbox) or microVM, not a shared long-lived container. This is an argument
  *for* the GKE on-demand tier over Cloud Run for this agent specifically. It's also
  long-running (~25s SSE heartbeat) → LB/idle timeouts must tolerate long requests.
- **CI/CD + image registry.** Move image builds to GitHub Actions → **Artifact Registry**,
  decoupling from the personal Docker Hub account (team blocker). Interim step already in
  motion (7-10): migrate images to a **shared Docker Hub account** (Adith's new creds) so
  Ronak can dev in parallel; Artifact Registry is the GCP end-state. The custom-agent lane
  already has a partial GH-Actions build in `agent-directory`; extend to the 6 specialists
  + infra images.
- **Networking (flagged the *most complex* part of the migration, 7-10).** Replicate the
  current port-based inter-container comms inside a **GCP VPC**, and restrict ingress to
  **WashU's VPN IP range** (the existing IP-masking/VPN posture Amaan already runs can be
  replicated). Control-plane services get internal networking; only the gateway is public.
  This is the item most likely to eat time — scope it early in Phase 0.
- **Two agent integration pathways (7-10, Dan).** Support both: (1) **agent-skeleton**
  agents the team builds/deploys (existing registration-service lane), and (2) **external
  API-endpoint** agents already hosted elsewhere (e.g. BioMni) — register just the endpoint,
  no build/spawn. Pathway (2) is naturally on-demand-exempt (someone else runs it) and is a
  clean fit for the "publish a card without running the agent" primitive (§9). The approval/
  admin-review gate stays the security boundary for third-party code (pathway 1).

---

## 7. Open questions / decisions to resolve before building

1. **On-demand mechanism — DECIDED (2026-07-13): Cloud Run per agent.** Free
   scale-to-zero + activation + stable URL; no current agent needs a GPU once LLM is on
   Vertex; lowest ops for the ~4-week window. **GKE is the graduation path** for the first
   agent that declares a GPU/TPU need or needs strict network-egress isolation. (Reverses
   the earlier GKE lean — the Vertex decision removed the GPU driver for it.) *§2.2*
2. **LLM tier — RESOLVED (2026-07-13):** managed Gemini via Vertex AI is primary; the
   ENGR H100 + `gpt-oss-120b` vLLM job stays as the standing backup (base-URL flip). *§4*
3. **Identity — RESOLVED (2026-07-13):** WashU SSO stays; **Option 1** (keep the
   Shibboleth proxy in front of a stable URL) for milestone 1; **Option 2** (Workforce
   Identity Federation + IAP) noted as a planned future hardening step. *§5*
4. **ADS home:** Cloud Run (min-instances=1) vs a small GKE Deployment — driven by
   whether libp2p routing + the OCI store need real persistence/networking. Verify
   whether the `localhost:5000` OCI registry ref in `ads_daemon.yaml` needs a
   companion registry service on GCP. *§2.1*
5. **Teardown granularity:** per-query vs per-session vs idle-timeout. *§3.2*
6. **Scope of milestone 1:** do we bring up **all 6 specialists** on-demand, or prove
   the loop end-to-end with **one simple agent** (e.g. disaster — already Chat-ready,
   no external secrets) first? Recommend the latter as the vertical slice.
7. **Budget:** $5k credits is modest. Scale-to-zero everywhere, min-instances=0 by
   default, GPU only when a card requires it, and turn the demo off between sessions.
   Need a rough cost model per option once §7.1/§7.2 are decided.

---

## 8. Suggested phasing (build order)

Each phase is independently demoable; earlier phases de-risk later ones.

> Maps to the §0 split: **Phases 0–1b = the 7-10 "control-plane serverless" goal**
> (the immediate Monday-review + hackathon deliverable); **Phases 2–4 = the 7-08
> "on-demand agents" goal** ($5k prototype).

- **Phase 0 — GCP foundation.** Project + GCP access (blocker — request immediately),
  **VPC + WashU-IP restriction** (flagged the *most complex* part of the migration),
  IAM, Artifact Registry, Cloud SQL instance, Secret Manager entries, budget alerts /
  FinOps, CI (GitHub Actions → Artifact Registry, off the personal Docker Hub account).
  No app logic. *Deliverable:* infra reachable, images pushed to Artifact Registry.
- **Phase 1 — Control plane on GCP serverless (the near-term goal).** Planner +
  registration on **Cloud Run**, ADS as an always-on service, **Cloud SQL**, wired to
  **Vertex** (§4). Agents still always-on for now (parity, not yet per-query). WashU
  Shibboleth proxy repointed at the stable planner URL (§5 Option 1). *Deliverable:* a
  query works end-to-end on GCP behind WashU login.
- **Phase 1b — Load testing (hackathon prerequisite).** Drive the **API** (not the UI)
  with GCP-spawned client VMs mimicking ~50+ concurrent users; find the bottleneck
  (planner autoscaling, Cloud SQL transactions, Vertex rate limits). *Deliverable:* a
  known concurrency ceiling + tuning (min-instances, max-instances, concurrency, Cloud
  SQL sizing).
- **Phase 2 — Decouple publishing.** Pre-publish the full catalog to ADS
  independent of agent processes; remove specialists' self-publish/atexit. *Deliverable:*
  planner discovers an agent that isn't running.
- **Phase 3 — On-demand backend + activation (the prototype).** New `GKEBackend`
  (or `CloudRunBackend`), card resource field, planner activate-and-wait. *Deliverable:*
  **milestone 1** — one right-sized instance spun up per query for the vertical-slice
  agent, then torn down.
- **Phase 4 — Fleet + LLM finalization.** Bring remaining specialists onto the
  on-demand path; finalize LLM tier (and any email/geospatial rewrites the choice
  forces). *Deliverable:* all agents on-demand.
- **Phase 5 — Identity, auth, secrets hardening.** IAP/Shibboleth decision, A2A
  authn/authz, Secret Manager fully wired, coding-agent sandboxing. *Deliverable:*
  shareable, authenticated demo URL.
- **Phase 6 — Cutover *evaluation* (Slurm stays a fallback).** Per the guiding
  principle, do **not** retire `submit.sbatch`/`start_vllm.sbatch`. Instead: run GCP and
  Slurm in parallel, document both paths, and only make GCP the *default* demo target
  once it's proven stable. The ENGR cluster path remains a maintained, working backup
  indefinitely for this prototype. No files deleted.

---

## 9. What to reuse (don't rebuild)

- `registration_service` `ContainerBackend` abstraction + `Supervisor` +
  `port_pool` + `agents` table + crash-recovery loop — the on-demand backend slots in
  here.
- The custom-agent register→approve→build→`image_ready` pipeline — only the *launch*
  step changes (host-`srun`-loop → GCP API).
- `ads_utils/publisher.py` (canonical push→verify→route path) and
  `agent_directory_service/scripts/publish_agents.py` (publish-without-running) — the
  pre-publish catalog primitive.
- The planner's agent-agnostic 5-tool design and A2A/OASF contracts — unchanged.
- `AGENT_INTEGRATION_SPEC.md`'s dependency-class tags — the per-item migration checklist.
