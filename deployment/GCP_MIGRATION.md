# GCP Migration — AgenticNetwork

**Amaan · updated 2026-07-21 · single source of truth for the GCP migration.**
Supersedes `GCP_MIGRATION_PLAN.md`, `GCP_MIGRATION_PROPOSAL.md`, `GCP_TEST_STATUS.md`, and
`GCP_GLOSSARY.md` — retired to avoid stale/conflicting copies. Diagram:
[`gcp_target_architecture.svg`](gcp_target_architecture.svg). Kept separate (broader than
GCP): [`DOWNSTREAM_CONSIDERATIONS.md`](DOWNSTREAM_CONSIDERATIONS.md) (cloud-agnostic scaling
log), [`AGENT_INTEGRATION_SPEC.md`](AGENT_INTEGRATION_SPEC.md) (agent-authoring reference),
[`../CLAUDE.md`](../CLAUDE.md) (whole-system overview).

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

**Connector-range gotcha (bites at planner-deploy time):** the default `default-allow-internal`
rule only covers `10.128.0.0/9`. If the Serverless VPC Access connector you create for the planner
lands *outside* that range (e.g. `10.8.0.0/28`), it **won't** be able to reach the relay's `:8100`
and the planner will fail to hit vLLM. Fix by either giving the connector a range inside
`10.128.0.0/9`, or adding an explicit rule:
`gcloud compute firewall-rules create allow-vllm-relay-from-connector --network=default
--direction=INGRESS --action=ALLOW --rules=tcp:8100 --source-ranges=<connector-range>
--target-tags=vllm-relay` (tag the VM `vllm-relay` first).

**Gotchas hit (for the record):** the served model name is `openai/gpt-oss-120b` (raw OpenAI
clients send it verbatim — must match vLLM's `--served-model-name`); the token is symmetric (same
value in `secrets.env`/`--api-key` and the planner's `OPENAI_API_KEY`); connect to
`shell.engr.wustl.edu`, not the node hostname you land on.

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
| **A1** | Control plane on Cloud Run + Cloud SQL + Vertex, behind WashU login (agents still always-on) | End-to-end query on GCP |
| **A1b** | Load test — ~50 users via API; tune Cloud Run + Cloud SQL | Known concurrency ceiling |
| **B2** | Decouple publishing | Planner discovers a stopped agent |
| **B3** | On-demand backend + activation (**milestone 1**) — stand up Cloud Run *and* GKE Autopilot in parallel; card resource field picks the backend per agent | One agent spun up per query, torn down |
| **B4** | Fleet + LLM finalize (email/geospatial Responses→Chat rewrites) | Full fleet on-demand |
| **C5** | Hardening — A2A auth, Secret Manager, coding-agent sandboxing | Secured demo URL |
| **C6** | Cutover eval — GCP default once proven; **Slurm stays** | Parallel run, no files deleted |

## First steps — getting *something* live on GCP

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
4. **Deploy the planner alone, self-contained.** Reuse the existing planner image (build
   `--platform linux/amd64`, push to the shared Docker Hub). `gcloud run deploy` with:
   `AGENT_NETWORK_FAKE_ADS=true` + baked cards, `PLANNER_ALLOW_ANON=1` (no DB/auth yet),
   `MAIN_AGENT_PORT=$PORT` (the code reads this, not `$PORT`), the LLM endpoint from step 3,
   `--min-instances=1 --cpu-always-allocated` (for the 60s background loop), `--max-instances`
   set. → a live HTTPS chat UI reasoning through the LLM, with no ADS/DB/proxy. **This is
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

- **ADS home:** Cloud Run (min=1) vs a small GKE Deployment — depends on whether libp2p
  routing + the OCI store need more persistence than Cloud Run offers. Verify whether the
  `localhost:5000` OCI registry ref in `ads_daemon.yaml` needs a companion registry service
  on GCP.
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
