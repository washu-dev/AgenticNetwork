# GCP Migration — AgenticNetwork

**Amaan · updated 2026-07-27 · single source of truth for the GCP migration.**
Supersedes `GCP_MIGRATION_PLAN.md`, `GCP_MIGRATION_PROPOSAL.md`, `GCP_TEST_STATUS.md`, and
`GCP_GLOSSARY.md` — retired to avoid stale/conflicting copies. Diagram:
[`gcp_target_architecture.svg`](../../../deployment/gcp_target_architecture.svg). Kept separate (broader than
GCP): [`DOWNSTREAM_CONSIDERATIONS.md`](../DOWNSTREAM_CONSIDERATIONS.md) (cloud-agnostic scaling
log), [`AGENT_INTEGRATION_SPEC.md`](../AGENT_INTEGRATION_SPEC.md) (agent-authoring reference),
[`../CLAUDE.md`](../../../CLAUDE.md) (whole-system overview).

---

## The goal in one sentence

Move the agent network off the WashU ENGR Slurm cluster onto GCP so it can **scale for
concurrent users** and **run agents on-demand** (spin up per query, tear down) instead of
24/7 — **additively**, keeping Slurm as a working backup.

## Two tracks (don't conflate them)

| Track | Scales *what* |
|---|---|
| **A — Control plane** | Planner + registry, for concurrent **users** |
| **B — On-demand agents** | Agents, right-sized **per query** |

## Why the move — what breaks on-demand today

The current deployment (`submit.sbatch`) runs 10 containers as parallel `srun --overlap`
steps on one Slurm node, all on `localhost`. The properties that don't survive an
on-demand/cloud move:

- Agents self-publish their card to ADS on boot and unpublish on exit — a scaled-to-zero
  agent vanishes from the directory and can never be re-activated (the dominant blocker).
- Card URLs are LAN IPs / `127.0.0.1:<port>` — no shared localhost across cloud instances.
- The vLLM endpoint is an ephemeral Slurm hostname pasted into `.env` each run.
- Persistent state lives on `/engrfs` bind-mounts; secrets live in `.env` + baked images.
- Auth is an out-of-repo Apache+Shibboleth gateway that must be repointed at a stable URL
  every run.

---

## Target architecture

```
  Browser ─HTTPS─▶ WashU Shibboleth gateway ─▶ Cloud Run: PLANNER (autoscales)
                   (existing, repointed once)        │ discover / dispatch
                          ┌─────────────────────────┼──────────────────────────┐
                          ▼                          ▼                           ▼
               Cloud Run: ADS            Cloud Run: REGISTRATION      ON-DEMAND AGENT TIER
               (directory, min=1)        (singleton, min=max=1)     backend chosen per card:
                          │                          │              Cloud Run (min=0, most agents)
                          │                          │              or GKE (GPU / strict isolation)
                          └───────────┬──────────────┘                          │
                                      ▼                                          │
                            Cloud SQL (Postgres) ◀──────────────────────────────┘

   LLM: ENGR vLLM gpt-oss-120b via autossh relay VM (for now)   [future: Vertex AI Gemini — base-URL flip]
   Platform: Secret Manager · Docker Hub (shared, external) · Cloud Logging · VPC (WashU-IP restricted)
```

**Why this shape:** the control plane (planner/registration/ADS) is stateless-per-turn and
serverless-friendly → Cloud Run. Agents become one service each, scale-to-zero where
possible, so activation-on-request *is* the on-demand mechanism and the service's stable URL
*is* the agent's directory address.

**Why one service per agent, not a shared pool:** each agent already has its own
image/dependencies (the environments "even disagree on library versions" — CLAUDE.md §5),
scale-to-zero is a per-service property (a shared service would stay warm whenever *any*
agent inside it is in use, defeating the point), and each agent's card resource field (see
"three load-bearing code changes" below) sizes CPU/mem/GPU independently. The pattern is
generic — it applies uniformly to the 6 specialists and to any future custom/third-party
agent, with no diagram or design change needed per agent added.

**Why two backends, chosen per card, from day one:** the original framing treated GKE as an
emergency escape hatch ("adopt when an agent needs a GPU"). But if more powerful agents are
actually on the roadmap, that trigger is a near-certainty, not an edge case:
- **Resource ceiling.** Cloud Run instances cap out around 8 vCPU / 32 GiB memory (reverify
  against current docs — these move) and single-L4-GPU-per-instance — fine for today's
  API-calling fleet, but a future agent doing local inference or heavier compute could hit
  that ceiling outright. GKE is the only path to arbitrary machine types and multi-GPU.
- **Isolation.** The same argument already applies to `coding_agent`'s YOLO bash mode — more
  capable/autonomous agents mean a bigger blast radius if the container boundary is ever
  escaped.

So: stand up a minimal GKE Autopilot cluster *in parallel* with Cloud Run at Phase B3, even
before any agent strictly needs it, and treat backend selection as a per-card config choice
(`ContainerBackend` already supports pluggable backends). The tradeoff: Autopilot isn't truly
zero-idle like Cloud Run — it bills per scheduled Pod, not per idle node, so it stays cheap
while unused, but it's not free, and it's one more thing the team learns during the same
~4-week window.

## Decisions (locked)

| Area | Decision | Why |
|---|---|---|
| **LLM (for now)** | ENGR `gpt-oss-120b` via vLLM, reached from GCP through an **autossh relay VM** tunneling to the static `h100-2405` node | Supervisor direction (2026-07-22): reuse the existing cluster LLM. No app code change (base-URL flip), ~$0 LLM cost, no Vertex token wiring. Relay is self-owned (no gateway/EIT dependency). **Working 2026-07-23.** See "vLLM endpoint from GCP" below. |
| **LLM (future)** | Vertex AI managed Gemini (`gemini-2.5-flash`, `us-central1`) | Validated 2026-07-14 (laptop + ENGR node); deferred, not removed — a base-URL flip when wanted. Also unblocks geospatial vision (native multimodal). |
| **Compute — control plane** | Cloud Run (planner autoscale, ADS min=1, registration min=max=1) | Stateless-per-turn control plane is a textbook serverless fit. |
| **Compute — agents** | Cloud Run *and* GKE, backend chosen per card | See "why two backends" above — anticipates more powerful future agents rather than reacting to the first one that needs it. |
| **Database** | Cloud SQL for PostgreSQL | Managed; replaces the `/engrfs` bind-mount. |
| **Registration svc** | Pinned Cloud Run singleton (`min=max=1`) | Owns lifecycle loops (health, build-poll); never needs to scale horizontally, just be a correct singleton. |
| **Login** | Keep WashU Shibboleth, repoint at the stable URL | Zero code change; preserves WashU SSO. Lock the back door (internal-only ingress) — see Auth below. |
| **Images** | Shared Docker Hub account (external) | Personal-account blocker already solved (2026-07-10). Artifact Registry deferred — revisit only if Docker Hub rate limits bite once on-demand testing means frequent cold-start pulls. |
| **Slurm** | Stays as maintained backup | De-risks the prototype; nothing deleted. |
| **Infrastructure-as-code** | Define the GCP stack in IaC (Pulumi-in-Python or Terraform/OpenTofu) | Reproducible spin-up/tear-down (key for cost control on fixed credits), reviewable, no click-ops drift. Note: IaC buys *repeatability*, not provider-agnosticism — real portability comes from the containers + standard interfaces (OpenAI-compat LLM, Postgres, HTTP/A2A) we already have. Adopt incrementally: `gcloud` scripts for the first deploys, capture in IaC once the stack shape settles. |

## Portability / no lock-in (design rule)

**Goal: the same system runs on the ENGR Slurm cluster *or* on GCP (or hybrid) — no hard coupling to
either.** Portability comes from **standard interfaces + containers**, not from any cloud tool or IaC.
The rule: keep platform-specifics at the *edges* (entrypoints, env wiring, IaC); keep app code
talking only to standard seams.

| Seam | Standard interface | Cluster | GCP |
|---|---|---|---|
| LLM | OpenAI-compatible Chat Completions | vLLM via `OPENAI_BASE_URL` | Vertex via `OPENAI_BASE_URL` |
| Agent↔agent | A2A over HTTP/JSON-RPC | localhost ports | Cloud Run URLs (card data) |
| State | Postgres wire protocol | Postgres on `/engrfs` | Cloud SQL |
| Packaging | OCI containers | Slurm (Pyxis/enroot) | Cloud Run / GKE |
| Launch (on-demand) | `ContainerBackend` interface | host `srun` loop | `CloudRunBackend` / `GKEBackend` |
| Config / secrets | env vars | `secrets.env` | Secret Manager → env |

**Rules that keep it un-locked-in:**
- **Cloud-SDK calls only behind config-gated adapters, never in the core path.** Example: the planner's
  Vertex support (`llm_client.py` `_make_client` / `_vertex_access_token`) and the disaster agent's
  (`_call_model`) both mint a Google token **only** when `…LLM_PROVIDER=vertex`; otherwise the plain
  OpenAI/vLLM path runs and the Google code never executes. Same image, backend chosen by env.
- **No hardcoded GCP project IDs / URLs / endpoints in app code** — they live in env
  (`OPENAI_BASE_URL`, `MAIN_AGENT_PORT` not `$PORT`, no baked Vertex URL).
- **One image runs both places;** only the *entrypoint* + *env wiring* differ (`submit.sbatch` vs a
  Cloud Run command override).
- **Slurm stays a maintained backup** — the migration is additive, not a boat-burn.

Portability wrinkles to retire (not GCP lock-in — cluster-origin assumptions that bite on both):
the planner/vendored copies shelling out to the `dirctl` binary, and ADS's `localhost:5000` OCI
registry assumption. Replace with a network directory client when standing up real ADS (Phase 2).

## LLM hosting

**Decision (2026-07-22, supervisor direction): for now, the GCP deployment calls the existing
ENGR vLLM** (`gpt-oss-120b`, OpenAI-compatible Chat Completions), *not* Vertex. The planner and
most agents already speak this API, so it's a base-URL flip with no app code change and ~$0 LLM
cost. Vertex stays the validated *future* path (a base-URL flip when wanted). The mechanics of
reaching the cluster LLM from GCP are below.

Options that were compared (kept for the record):

| Option | What it is | GPU ops | Cost shape | Status |
|---|---|---|---|---|
| ENGR vLLM via gateway proxy | Existing cluster `gpt-oss-120b`, reached through the `agenticweb` Apache proxy | None (reuses running H100) | ~$0 (already running) | ✅ For now |
| Managed — Vertex AI Gemini | Google's hosted Gemini via Vertex's OpenAI-compat endpoint | None | Pay-per-token, $0 idle | Deferred (validated) |
| Self-host vLLM on Cloud Run / GKE GPU | vLLM packaged on a GCP GPU | Low–high | GPU-hour while warm | Not pursued |

**Vertex (deferred) — what was validated 2026-07-14** (laptop + ENGR node): `us-central1` +
`google/gemini-2.5-flash`, token via `gcloud auth print-access-token`, ENGR→`*.googleapis.com:443`
egress open. Credits apply through Vertex (not the AI-Studio API). Gotchas for whenever it's
adopted: `gemini-2.0-flash-001` discontinued 2026-06-01 (use `gemini-2.5-flash`); Gemini-3.x needs
the `global` endpoint (not regional), reopening the single-region/egress call; and Vertex's
`api_key` is a short-lived (~1h) token needing a refresh helper. Adopting Vertex later also
unblocks geospatial's vision path (native multimodal).

### vLLM endpoint from GCP — the autossh relay VM (chosen, working 2026-07-23)

The GCP planner can't reach the cluster's internal vLLM node directly (campus firewall). We bridge
it with a **small always-on GCE VM that holds a persistent SSH tunnel into the cluster** and
presents a stable endpoint on the GCP VPC:

```
Cloud Run planner ─(VPC)─▶ relay VM :8100 ─(autossh -L)─▶ shell.engr.wustl.edu ─▶ h100-2405:8100 (vLLM)
```

The vLLM node is **static** (`h100-2405`), so the tunnel target never changes; only the vLLM Slurm
job's uptime varies. This path is **entirely self-owned** — no gateway edits, no EIT tickets.

**Why not the `agenticweb` Apache gateway (rejected 2026-07-23):** we first tried exposing vLLM as
a `/vllm/` reverse-proxy path on the existing Shibboleth gateway. It fought back on two fronts —
the root-owned Apptainer/Apache instance was fragile to edit/reload, and (decisively) the **EIT
nginx front terminates TLS and enforces Shibboleth SSO for the whole vhost**, redirecting `/vllm/`
to login before it ever reached Apache. Exempting a path there is an EIT change, not ours. The
relay VM sidesteps both.

**Setup (verified working):**
1. Create a tiny VM: `gcloud compute instances create vllm-relay --zone=us-central1-a
   --machine-type=e2-micro --image-family=debian-12 --image-project=debian-cloud` (internal IP
   e.g. `10.128.0.2`).
2. Authorize an SSH key from the VM into the cluster (its public half in `~/.ssh/authorized_keys`
   on `shell.engr.wustl.edu` — the load-balanced login alias, **not** a specific `ssh-shell-N`).
3. On the VM, run autossh under **systemd** (so it survives reboots; autossh handles tunnel drops):
   `autossh -M 0 -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -i ~/.ssh/id_ed25519
   -L 0.0.0.0:8100:h100-2405.engr.wustl.edu:8100 amaan@shell.engr.wustl.edu`.
4. Planner sets `OPENAI_BASE_URL=http://<relay-internal-ip>:8100/v1`,
   `OPENAI_API_KEY=<token>`, `MAIN_AGENT_LLM_MODEL=openai/gpt-oss-120b` (verbatim served name).

**Security / firewall:** vLLM runs with `--api-key <token>` (in `deployment/secrets.env`, sourced
by `start_vllm.sbatch`); the planner sends it as `OPENAI_API_KEY`. `:8100` must never have public
ingress — GCP is default-deny, and with no `tcp:8100` allow rule it's already closed to the
internet (verify: `curl` the VM's *external* IP:8100 from off-VPC → should time out). The token is
the primary control; the firewall is defense-in-depth.

**Connector-range gotcha (bites at planner-deploy time):** in an auto-mode VPC the entire
`10.128.0.0/9` block is **reserved** for auto-subnet allocation, so the Serverless VPC Access
connector's `/28` **must** live *outside* it (e.g. `10.8.0.0/28`, in `10.0.0.0/9`) — any range
inside `10.128.0.0/9` fails to create with "conflicts with an existing subnetwork." But a range
outside `10.128.0.0/9` is *not* covered by `default-allow-internal`, so you **must** also add an
explicit rule for the relay to accept the connector's traffic (this is not optional):
`gcloud compute firewall-rules create allow-vllm-relay-from-connector --network=default
--direction=INGRESS --action=ALLOW --rules=tcp:8100 --source-ranges=10.8.0.0/28
--target-tags=vllm-relay` (tag the VM `vllm-relay` first). Without the rule the planner deploys
fine but every LLM call silently hangs.

**Gotchas hit (for the record):** the served model name is `openai/gpt-oss-120b` (raw OpenAI
clients send it verbatim — must match vLLM's `--served-model-name`); the token is symmetric (same
value in `secrets.env`/`--api-key` and the planner's `OPENAI_API_KEY`); connect to
`shell.engr.wustl.edu`, not the node hostname you land on.

### Planner on Cloud Run — first live slice (working 2026-07-27)

The planner runs **self-contained on Cloud Run** (fake ADS, no DB, no auth), reasoning through the
ENGR vLLM via the relay. Definition of done met: chat UI loads, `gpt-oss-120b` answers, `/health`
→ `200`. This is the "something live on GCP" vertical slice; real ADS / Cloud SQL / Shibboleth
layer on after (Phasing A1→). The request path is
`Cloud Run planner ─(connector)─▶ relay :8100 ─(autossh)─▶ h100-2405:8100 (vLLM)`; the planner
carries the token as `OPENAI_API_KEY`, which vLLM checks app-side (the relay is a dumb TCP tunnel
and does **not** inject it).

**Working sequence** (`di2-agenticweb`, `us-central1`):
1. **Token → Secret Manager.** `printf %s '<token>' | gcloud secrets create vllm-api-key
   --data-file=-` — use `printf %s`, **never** `echo` (a trailing `\n` breaks the bearer match →
   401; verify with `gcloud secrets versions access latest --secret=vllm-api-key | xxd | tail`,
   last byte must not be `0a`). Then grant the runtime SA read access:
   `gcloud secrets add-iam-policy-binding vllm-api-key
   --member="serviceAccount:<project-number>-compute@developer.gserviceaccount.com"
   --role="roles/secretmanager.secretAccessor"`.
2. **VPC connector** so Cloud Run can reach the relay's private IP:
   `gcloud compute networks vpc-access connectors create planner-connector --region=us-central1
   --network=default --range=10.8.0.0/28` (range **must** be outside `10.128.0.0/9` — see gotcha).
3. **Firewall** the relay to the connector range (required — see connector-range gotcha above): tag
   the VM `vllm-relay`, then allow `tcp:8100` from `10.8.0.0/28` to that tag.
4. **Deploy** with a command override that bypasses the entrypoint's ADS-wait:
   `gcloud run deploy planner --image=di2washu/agenticnetwork-planner:latest --port=8080
   --command=python --args=-m,planner_agent.scripts.run_server,--fake-ads
   --vpc-connector=planner-connector
   --set-env-vars=MAIN_AGENT_PORT=8080,AGENT_NETWORK_FAKE_ADS=true,PLANNER_ALLOW_ANON=1,OPENAI_BASE_URL=http://10.128.0.2:8100/v1,MAIN_AGENT_LLM_MODEL=openai/gpt-oss-120b
   --set-secrets=OPENAI_API_KEY=vllm-api-key:latest --min-instances=1 --max-instances=1
   --no-cpu-throttling --memory=1Gi --timeout=3600 --no-allow-unauthenticated`.
5. **Test privately** via `gcloud run services proxy planner --region=us-central1 --port=8080` →
   browser `http://localhost:8080/` (send a message; "no agents" is expected in fake-ADS mode) and
   `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/health` → `200`.

**Gotchas that actually bit (beyond the vLLM ones above):**
- **Editor can't grant the secret binding.** Creating the secret only needs `roles/editor`, but
  `secrets add-iam-policy-binding` needs `secretmanager.secrets.setIamPolicy`, which is Owner /
  `secretmanager.admin`-only — a fresh operator hits a 403 and must first get `roles/secretmanager.admin`
  from a project Owner. Deploy also can't self-grant it. Request the role up front.
- **Connector range must be *outside* `10.128.0.0/9`** and paired with the `tcp:8100` firewall rule
  — full detail in the connector-range gotcha above.
- **`MAIN_AGENT_PORT=8080`, not `$PORT`** (the code reads `MAIN_AGENT_PORT`); `OPENAI_API_KEY` is
  mandatory at boot (the vLLM token satisfies it); `--no-cpu-throttling` for the 60s background
  health loop; the command override is required (the stock entrypoint waits for ADS and exits);
  keep `--no-allow-unauthenticated` while `PLANNER_ALLOW_ANON=1` (otherwise it's an open, no-login
  planner driving the ENGR LLM — proxy for testing, don't expose it).

### Operating the live planner — cheat-sheet

**What's up right now (5 GCP resources + 1 ENGR dependency):** Cloud Run `planner` (min=1, private,
always-warm) · VPC connector `planner-connector` (`10.8.0.0/28`, always-on) · firewall
`allow-vllm-relay-from-connector` · GCE VM `vllm-relay` (e2-micro, always-on) · Secret `vllm-api-key`
· **ENGR vLLM Slurm job on `h100-2405`** (the one leg not controlled from GCP — if it ends, the UI
still loads but every message fails at the LLM call). All commands take
`--region=us-central1 --project=di2-agenticweb`.

**Inspect / access:**
```
gcloud run services describe planner --region=us-central1 --project=di2-agenticweb
gcloud run services logs read planner --region=us-central1 --project=di2-agenticweb --limit=80
gcloud run services proxy planner --region=us-central1 --port=8080 --project=di2-agenticweb   # → http://localhost:8080/ (only way in)
```

**Change config / ship code:** `gcloud run services update planner ... --update-env-vars=K=V` for a
tweak; re-run the full `gcloud run deploy planner ...` (above) to push a new image (new revision,
auto traffic shift).

**Diagnose a failed LLM call** (problem is the relay/Slurm job, not Cloud Run):
```
gcloud compute ssh vllm-relay --zone=us-central1-a --project=di2-agenticweb \
  --command="systemctl is-active vllm-tunnel && curl -s -o /dev/null -w '%{http_code}\n' localhost:8100/v1/models"   # want: active + 200
```

**Cost note — this slice is NOT scale-to-zero.** Planner (min=1 + always-CPU), the connector (≥2
always-on instances), and the relay VM all bill while idle. To pause spend between demos (all
reversible): `gcloud run services update planner ... --min-instances=0` (planner cold-starts on
demand) and `gcloud compute instances stop vllm-relay --zone=us-central1-a ...`. The connector can't
be paused — delete + recreate it if it'll sit idle for a long stretch. For a short-lived demo, leave
everything up.

## Identity, gateway & auth

Three independent questions, three different owners:

| Layer | Question | Where it lives | Status |
|---|---|---|---|
| **1. User login** | Is this a real WashU person? | Gateway in front of the planner | WashU Shibboleth (working) — repoint at the stable Cloud Run URL |
| **2. Service-to-service (A2A)** | May this planner call this agent? | Planner ↔ agents | None yet — network-level isolation (internal VPC ingress) for milestone 1 |
| **3. Cloud / LLM auth** | May this app call Vertex? | Planner → Google APIs | Service account via Workload Identity — short-lived token, auto-refreshed |

**Login (must-do):** the planner has no login code of its own — it blindly trusts
`eppn`/`mail`/`remote-user` headers injected by the external Apache+`mod_shib` gateway. So
the planner's Cloud Run ingress **must** be locked to internal-only (only the proxy can reach
it) — otherwise anyone who reaches it directly can forge those headers and impersonate any
WashU user. The gateway runs as a sidecar in the planner's Cloud Run revision (ingress
container), forwarding validated headers over `localhost` to the planner.

**Future hardening (not now):** replace the bespoke Apache/Shibboleth box with Workforce
Identity Federation + IAP (Google-managed login, still WashU-credentialed) — bigger one-time
setup + WashU IT coordination, revisit when retiring the external box becomes worthwhile.

## Cross-cutting hardening backlog

- **Secrets → Secret Manager.** Move `CREDENTIALS_ENCRYPTION_KEY`, `GITHUB_TOKEN`, Google
  OAuth client, encrypted per-user credentials. Remove baked email Google creds from the
  image; externalize email's on-disk OAuth refresh tokens to the shared DB.
- **Directory client, not `dirctl` subprocess.** Planner (and coding/intelligence vendored
  copies) shell out to the `dirctl` binary — replace with a network/gRPC client so a
  serverless pod needs no binary + no local daemon.
- **Unify the two directories.** Reconcile scout's gRPC `AgentDirectory` (`:8889`) with
  `dirctl` (`:8888`); collapse the 5+ duplicate ADS-address env vars.
- **Stable service identities.** Replace the 5-digit port remaps and advertise-url≠bind-port
  inconsistencies with per-agent DNS/service names.
- **`coding_agent` isolation.** YOLO bash mode needs per-request sandboxing — this is the
  concrete case for the GKE backend (fresh Pod per request), not just a hypothetical. A
  cheaper stopgap: `concurrency=1` on its Cloud Run service plus verified scratch-dir cleanup
  between requests.
- **CI/CD.** GitHub Actions building images to the shared Docker Hub account now; Artifact
  Registry deferred (see Decisions).
- **Networking — flagged the hardest part.** Replicate today's port-based comms inside a GCP
  VPC, restrict ingress to WashU's IP range. Scope early (Phase A0).
- **Two agent integration pathways.** (1) agent-skeleton agents the team builds (existing
  registration-service lane), (2) external API-endpoint agents already hosted elsewhere
  (e.g. BioMni) — register just the endpoint, no build/spawn, naturally on-demand-exempt.

## The three load-bearing code changes for on-demand (Track B)

1. **Decouple ADS publishing from agent lifetime** — pre-publish the full catalog so a
   stopped agent is still discoverable. Primitive already exists:
   `agent_directory_service/scripts/publish_agents.py`.
2. **On-demand backend + planner activate-and-wait** — a `CloudRunBackend`/`GKEBackend`
   implementing the existing `ContainerBackend` interface; planner brings the chosen agent up
   and polls health before dispatching.
3. **Card resource-requirements field** — developer-declared CPU/mem/GPU (extension field,
   mirroring the P4 credentials pattern) so the backend right-sizes the instance and picks
   Cloud Run vs GKE.

## What to reuse (don't rebuild)

- `registration_service` `ContainerBackend` abstraction + `Supervisor` + `port_pool` +
  `agents` table + crash-recovery loop — the on-demand backend slots in here.
- The custom-agent register→approve→build→`image_ready` pipeline — only the *launch* step
  changes (host-`srun`-loop → GCP API).
- `ads_utils/publisher.py` (canonical push→verify→route path) and `publish_agents.py`
  (publish-without-running) — the pre-publish catalog primitive.
- The planner's agent-agnostic 5-tool design and A2A/OASF contracts — unchanged.
- `AGENT_INTEGRATION_SPEC.md`'s dependency-class tags — the per-item migration checklist.

## Phasing

| # | Phase | Deliverable |
|---|---|---|
| **A0** | Foundation — GCP access *(blocker)*, VPC + WashU-IP restriction, Cloud SQL, CI (GitHub Actions → shared Docker Hub), budget alerts | Infra up; images pushed via CI |
| **A1** | Control plane on Cloud Run + Vertex (planner + real ADS; Cloud SQL/login still TODO) | End-to-end query on GCP — **✅ done** |
| **A1b** | Load test — ~50 users via API; tune Cloud Run + Cloud SQL | Known concurrency ceiling |
| **B2** | Decouple publishing | Planner discovers a stopped agent — **✅ done** (decoupled `ads_utils` publish) |
| **B3** | On-demand backend + activation (**milestone 1**) — Cloud Run native scale-to-zero (GKE deferred) | One agent spun up per query — **✅ done** (manual; `CloudRunBackend` automates it, Phase 4) |
| **B4** | Fleet + LLM finalize (email/geospatial Responses→Chat rewrites) | Full fleet on-demand |
| **C5** | Hardening — A2A auth, Secret Manager, coding-agent sandboxing | Secured demo URL |
| **C6** | Cutover eval — GCP default once proven; **Slurm stays** | Parallel run, no files deleted |

## Shipped — Phases 0-3 live on GCP (2026-07-29)

Phases **A1, B2, B3 are implemented and running** (`di2-agenticweb`, `us-central1`). The WIP runbooks
that drove them are folded here and removed. What's live:

- **Planner** on Cloud Run; LLM via Vertex with **vLLM-primary / Vertex automatic failover**
  (`llm_client.py` `_create_with_failover`; env `MAIN_AGENT_LLM_FALLBACK_*`). vLLM reached via the
  `vllm-relay` VM (autossh) + the `planner-connector` VPC connector.
- **Authenticated cross-network A2A** — the planner attaches a Google OIDC ID token for `*.run.app`
  targets (`transport.py` `CloudRunIdTokenAuth`); each agent's SA gets `run.invoker`. (Org policy blocks
  public `allUsers` Cloud Run — this replaced it.)
- **Real ADS** on a Container-Optimized-OS GCE VM (`ads`, `10.128.0.3`): `dirctl daemon` + a local OCI
  registry, sqlite config `ads_daemon.gcp.yaml`. Planner discovers dynamically (`dirctl` is in the
  planner image; set `ADS_SERVER_ADDR`, drop `AGENT_NETWORK_FAKE_ADS`).
- **On-demand agent** — the disaster agent at `min-instances=0`; Cloud Run cold-starts it per query.

### Operational gotchas (learned — these bit us)
- **COS host firewall.** Container-Optimized OS ships `iptables -P INPUT DROP` (only lo/icmp/ssh/
  established), so `:8888/:8999` are dropped even with the daemon listening + the VPC firewall open
  (symptom: `docker exec … localhost` works, VM-to-VM/Cloud Run time out). Fix: `iptables -I INPUT -p
  tcp -m multiport --dports 8888,8999 -j ACCEPT` + a startup-script (COS resets iptables on reboot).
- **VPC connector range must be *outside* `10.128.0.0/9`** (auto-mode reserves it) — use `10.8.0.0/28`
  — **and** needs an explicit `tcp:<port>` firewall rule (it's outside `default-allow-internal`).
- **`OPENAI_API_KEY` must equal `VLLM_API_KEY`** once vLLM is gated (`--api-key`) — derive it in
  `secrets.env`. A dummy only worked while vLLM was un-gated.
- **vLLM torch.compile cache must be node-local**, not `/engrfs` NFS (atomic-rename crash) — set
  `VLLM_CACHE_ROOT`/`TORCHINDUCTOR_CACHE_DIR`/`TRITON_CACHE_DIR` to `/tmp` in `start_vllm.sbatch`.
- **Stale OASF converter.** `AgenticNetwork`'s `card_oasf_converter`/`ads_utils` emit empty top-level
  `skills` for custom-skill cards → stored-but-not-routed → invisible. Publish via the
  `agent-directory/ads_utils` converter, or ensure an `oasf:<name>:<id>` tag on ≥1 skill.
- **On-demand publish must be decoupled** — a self-publishing agent unpublishes on scale-down (and
  crashes on boot if ADS is unreachable, since publish runs before it binds `$PORT`). Publish the card
  separately so it persists at 0 instances.
- **Planner health-probe cold-starts min=0 agents** every cycle → they never idle. Skip them
  (`HEALTH_PROBE_SKIP_AGENTS`; the `CloudRunBackend` will own this).
- **Cold-start timeout** — raise the planner's `A2A_HTTP_TIMEOUT_SECONDS` (120) so the first request
  doesn't time out while Cloud Run spins the agent up.
- **IAM (recurring):** Editor can create resources but not modify IAM (`*.setIamPolicy`) — each step
  needed a specific admin role (`secretmanager.admin`, `run.admin`) from a project Owner.

**Next — Phase 4 (B4/on-demand fleet): automate agent onboarding via a `CloudRunBackend`** so a
registered agent auto-deploys + auto-publishes. Design + steps:
[`DESIGN_CLOUDRUN_BACKEND.md`](DESIGN_CLOUDRUN_BACKEND.md),
[`IMPLEMENTATION_CLOUDRUN_BACKEND.md`](IMPLEMENTATION_CLOUDRUN_BACKEND.md). **In progress as of
2026-08-03: Steps 0-8 of 10 done** (code in the working tree; Step 7 IAM live on GCP) — capability-flag
refactor, OASF routable-skill enforcement, `image_ref` digest column, the **`CloudRunBackend` itself**
(deploy by digest, `:latest` fallback, planner-SA invoker grant, scale-to-zero, `image_ready`→deploy→
publish-run.app-URL trigger), Step 5's **decoupled-publish** contract, Step 6's **health-loop skip + Cloud
Run recovery reconcile**, Step 7's **IAM** (`agent-runtime` + `registration-service` SAs; captured in
`scripts/gcp_iam_step7.sh`), and Step 8's **resource-requirements card extension** (`resources` column +
card ext → cpu/memory/concurrency/max-instances deploy args; GPU→GKEBackend seam). Working tree rebased
onto the upstream `build_manifest` commit. 187 tests pass.
**Step 9 core PROVEN LIVE (2026-08-03):** the registration service runs on **Cloud Run** wired to **Cloud SQL**
(`agenticnetwork-db`) + the VPC connector, and drove a custom agent end-to-end — register → CI build →
`image_ready` → `CloudRunBackend` auto-deploy (`agent-echo-test`, min=0) → card published. First live Phase-4
loop. Three bugs fixed live (JSONB-string `resources`, recovery retry, Cloud Run entrypoint). The **planner is now
also on Cloud SQL** (shared `registration` DB) — **Step 9 complete: reg service + planner both on Cloud
Run/Cloud SQL.** Only **Step 10** (planner front-door) and the **Option-1 cutover** remain. See the
implementation doc's Step 9 notes.

## First steps — getting *something* live on GCP (historical — done)

The fastest path to a real GCP URL, ordered by least resistance. Steps 1–5 land a working
planner; everything after layers onto it (see Phasing). Do step 4 with plain `gcloud` first to
prove it, then capture the stack in IaC.

1. **Unblock access.** Get into the GCP project and enable the APIs you'll touch (Cloud Run,
   Cloud SQL, Secret Manager, Artifact Registry only if used). This is the one hard blocker.
2. **Set a spending guardrail first.** Create a budget + alerts on the billing account, and
   plan to pass `--max-instances` on every deploy. On fixed credits, the cap matters more than
   the alert (alerts notify, they don't stop spend).
3. **Stand up the LLM endpoint (ENGR vLLM via the autossh relay VM — done 2026-07-23).** Launch
   vLLM with an `--api-key`, create the relay VM, run autossh under systemd, and verify
   `curl localhost:8100/v1/models` on the VM returns `200` (see "vLLM endpoint from GCP" above).
   The planner in step 4 points `OPENAI_BASE_URL` at `http://<relay-internal-ip>:8100/v1`.
4. **Deploy the planner alone, self-contained (done ✅ 2026-07-27).** Working runbook folded into
   "Planner on Cloud Run — first live slice" above. Reuses the existing planner image
   (`di2washu/agenticnetwork-planner`, `linux/amd64`, shared Docker Hub), `AGENT_NETWORK_FAKE_ADS=true`
   + baked cards, `PLANNER_ALLOW_ANON=1` (no DB/auth yet), `MAIN_AGENT_PORT=8080`, the LLM endpoint
   from step 3, `--min-instances=1 --no-cpu-throttling --max-instances=1`, private ingress + proxy
   for testing. → a live HTTPS chat UI reasoning through gpt-oss-120b, no ADS/DB/proxy. **This is
   "something on GCP."**
5. **Capture it in IaC.** Once step 4 works, translate that one service into Pulumi/Terraform so
   it's reproducible and tear-down-able. Small surface = a good first IaC artifact.
6. **Then layer (Phasing A1→):** Cloud SQL → real ADS (a small VM is lower-resistance than
   forcing the gRPC daemon onto Cloud Run) → registration singleton → lock the front door
   (Shibboleth proxy + internal-only ingress).

## Cost & runway

~$220–320/mo for Track A (Cloud Run ~$180 · Cloud SQL ~$10–30 · networking ~$25–30). **LLM cost
is ~$0 while on the ENGR vLLM** (already-running H100); it becomes pay-per-token (~$30–150/mo)
only if/when we switch to Vertex. On-demand Cloud Run agents are near-$0 idle; GKE Autopilot
bills per scheduled Pod (cheap but not $0 while any agent is defined on it). **$5k credits →
~15+ months** at the Track A rate. Main levers: keep the LLM on ENGR (or Gemini **Flash** if on
Vertex), stub the LLM during load tests, stay **single-region**.

## Open questions

- **ADS home:** ~~Cloud Run vs GKE~~ **resolved — a small Container-Optimized-OS GCE VM** (`ads`) runs
  `dirctl daemon` + a co-located `registry:2` OCI store with host networking, sqlite config
  (`ads_daemon.gcp.yaml`). Lower-resistance than forcing the libp2p/OCI daemon onto serverless. (COS
  host-firewall gotcha: see "Operational gotchas.")
- **Teardown granularity:** per-query vs per-session vs idle-timeout for on-demand agents.
- **Milestone-1 scope:** bring up all 6 specialists on-demand at once, or prove the loop with
  one simple agent (e.g. disaster) first? Recommend the latter as the vertical slice.

## Validation status

*(project `di2-agenticweb` · region `us-central1`)*

**✅ Proven (2026-07-14) — Vertex AI LLM endpoint reachable from the cluster.** See "LLM hosting"
above. De-risks the (now deferred) Vertex path.

**✅ Working (2026-07-23) — GCP→ENGR vLLM via autossh relay VM.** A GCE `e2-micro` (`vllm-relay`,
internal `10.128.0.2`) runs autossh tunneling `:8100` → `shell.engr.wustl.edu` → `h100-2405:8100`;
`curl localhost:8100/v1/models` on the VM returns `200`. Self-owned, no gateway/EIT dependency.
**Remaining hardening:** run autossh under systemd (reboot-durable), and firewall `:8100` to the
Cloud Run VPC connector range only. The `agenticweb` Apache-gateway approach was tried first and
**rejected** — the EIT nginx front enforces SSO for the whole vhost above our Apache (see "vLLM
endpoint from GCP").

**✅ Working (2026-07-27) — Planner live on Cloud Run.** Self-contained planner (fake ADS, no
DB/auth) on Cloud Run in `di2-agenticweb`/`us-central1`, reaching the ENGR vLLM through the relay
via a Serverless VPC Access connector (`10.8.0.0/28`) + a `tcp:8100` firewall rule to the relay.
Chat UI loads, `gpt-oss-120b` answers, `/health` → `200`; tested privately via `gcloud run services
proxy`. First "something live on GCP" slice (Phasing A1, partial — auth / DB / real ADS still to
layer on). Runbook: "Planner on Cloud Run — first live slice" above.

**🔄 Next — decentralization test (runbook ready, not yet executed).** Deploy the
disaster-response agent on GCP Cloud Run, publish its card into the live ENGR ADS, confirm
the running agenticweb can discover and route to it. One passing run validates four things at
once: hybrid discovery, cross-network A2A (ENGR→GCP), publishing decoupled from agent
lifetime, and the LLM leg. The card is published *from ENGR* (pointing at the Cloud
Run URL) — the GCP agent doesn't self-publish, since it can't reach ENGR's directory inbound.

Note: test agents currently run unauthenticated + public on Cloud Run — acceptable for scoped
tests, must be torn down after; real deploy needs internal-ingress (see Auth above).

## Top risks

- **Networking is the hardest part** — VPC + WashU-IP inbound restriction (outbound to Google
  APIs already proven). Scope early in A0.
- **Cold-start latency** on first per-query agent call — warm-pool the hot agents.
- **email + geospatial** still need a Responses→Chat API rewrite (B4).
- **Team GCP ramp-up** — A0/A1 doubles as the learning curve.

---

## Appendix — glossary

- **Container / image** — a packaged app; an image is the frozen template, a container is a
  running copy.
- **Serverless** — hand the cloud a container; it decides when/how many copies run. No VMs to
  manage, pay only while working.
- **Scale-to-zero** — drop to zero running copies when idle → $0 idle cost. **Cold start** —
  the delay when a scaled-to-zero service boots on its first request.
- **Cloud Run** — fully managed serverless containers with a stable HTTPS URL. `min`/
  `max-instances` set the floor/ceiling of running copies; `concurrency` sets how many
  requests one copy handles at once (default 80, set to 1 for one-request-per-instance);
  "CPU always allocated" is needed for background work between requests.
- **GKE (Autopilot)** — managed Kubernetes; a Pod is the smallest run unit, a Node is the
  machine it runs on. Autopilot bills per Pod's requested CPU/mem rather than per node.
- **Vertex AI** — Google's managed AI platform; used here for managed Gemini inference via an
  OpenAI-compatible endpoint, billed pay-per-token.
- **Cloud SQL** — managed PostgreSQL; always-on (not scale-to-zero) since databases hold
  state.
- **Artifact Registry** — GCP's own container registry (same-region pulls free); currently
  deferred in favor of the shared Docker Hub.
- **Secret Manager** — secure vault for API keys/secrets, replacing `.env` files and
  baked-in creds.
- **VPC** — a private network inside GCP; used here to restrict access to WashU's IP range.
- **IAM / service account / Workload Identity** — GCP's permission system; a service account
  is a non-human app identity; Workload Identity lets a Cloud Run/GKE workload authenticate
  as one without a key file.
- **IAP (Identity-Aware Proxy)** — Google's managed login-enforcing front door; a future
  alternative to the self-run Shibboleth gateway.
- **Egress** — data leaving GCP or crossing regions, billed; staying single-region keeps
  internal traffic free.
