# Deployed components — what was live on GCP (updated 2026-08-03)

Operator reference for what runs on GCP. As of 2026-08-03 (not re-verified since; check with
`gcloud run services list` and `gcloud compute instances list` before relying on it) this is **Phases 0-3 + the
CloudRunBackend (Phase 4) live**: planner + real ADS + on-demand agent, **plus the registration service on
Cloud Run wired to Cloud SQL, auto-deploying agents end-to-end**. Full design/status:
[`MIGRATION.md`](MIGRATION.md) + [`IMPLEMENTATION_CLOUDRUN_BACKEND.md`](IMPLEMENTATION_CLOUDRUN_BACKEND.md)
(Step 9 notes). Everything is in project `di2-agenticweb`, region `us-central1`.

## The request paths

```
User path:
  you ─(gcloud run proxy, for now)─▶ Cloud Run: planner ──dirctl──▶ ADS VM (10.128.0.3:8888)  [discover]
                        │                                                    [route ↓ via ID token]
                        │  LLM (vLLM primary → Vertex fallback)   Cloud Run agents (min=0, e.g. disaster,
                        ▼                                          agent-echo-test) cold-start on demand
        vLLM-relay VM :8100 ─autossh─▶ h100-2405 (vLLM)          Cloud SQL (agenticnetwork-db) ◀─ planner
                                                                  persists users/creds/conversations

Onboarding path (Phase 4, automated):
  register agent ▶ Cloud Run: registration ─commit─▶ GitHub Action builds image ─▶ Docker Hub
                        │                                                              │
                        └─ build-poll sees image ─▶ CloudRunBackend deploys agent (min=0, as agent-runtime SA,
                           grants planner run.invoker) ─▶ publishes card to ADS ─▶ planner can discover it
```

## Components

**Cloud Run `planner`** — orchestrator + chat UI. Discovers via ADS (`dirctl` in-image,
`ADS_SERVER_ADDR=10.128.0.3:8888`), routes over authenticated A2A (Google OIDC ID token for `*.run.app`),
reasons via **vLLM-primary / Vertex-fallback**. **Now on Cloud SQL** (`REGISTRATION_DB_DSN` secret →
persists users/credentials/conversation history; `CREDENTIALS_ENCRYPTION_KEY` secret). Still
`PLANNER_ALLOW_ANON=1` + IAM-private (`ingress=all`, `--no-allow-unauthenticated`, reach via
`gcloud run services proxy`) — the real front door is Step 10 (see below). `min=1`, `--no-cpu-throttling`.

**Cloud Run `registration`** — the CloudRunBackend host. `SPAWN_BACKEND=cloudrun`, runs as the
`registration-service` SA. Wired to Cloud SQL (`--add-cloudsql-instances`, DSN in `registration-db-dsn`),
`--vpc-connector planner-connector` for ADS, `github-token` secret for the build-commit path,
`ADMIN_EMAILS=amaan@wustl.edu`. `ingress=all` + `--no-allow-unauthenticated` (IAM-private, reach via proxy).
**Singleton: `min=max=1`, `--no-cpu-throttling`** (holds in-memory backend state + the build-poll/recovery
loops). Env: `GCP_PROJECT`/`GCP_REGION`/`AGENT_RUNTIME_SA`/`PLANNER_INVOKER_SA`/`IMAGE_REGISTRY`,
`SKIP_STARTUP_WAITS=1`.

**Cloud SQL `agenticnetwork-db`** — POSTGRES_15, db-f1-micro, connection
`di2-agenticweb:us-central1:agenticnetwork-db`. Database `registration` is **shared** by the planner and
the reg service (disjoint tables; distinct trigger fns, no clobber). User `regsvc`; full socket DSN in
secret `registration-db-dsn`. Reached via Cloud Run's built-in Cloud SQL connector (auth-proxy socket) —
no public exposure.

**Cloud Run agents (on-demand, `min=0`)** — `disaster` (manual, from Phase 3) and `agent-echo-test` (a
**throwaway** deployed by the CloudRunBackend to prove the loop — safe to delete: `gcloud run services
delete agent-echo-test --region=us-central1`). Run as `agent-runtime` SA, internal ingress, planner granted
`run.invoker`. $0 idle.

**`ads` VM (directory)** — COS GCE VM (`10.128.0.3`): `dirctl daemon` + co-located `registry:2` (host
networking, sqlite `ads_daemon.gcp.yaml`). Ports 8888/8999 open in the COS host firewall (+ reboot
startup-script). Holds the routed agent catalog.

**`vllm-relay` VM** — always-on e2-micro (`10.128.0.2`), autossh tunnel (systemd) to `h100-2405:8100`.
Dormant when the planner is on the Vertex fallback.

**`planner-connector` (Serverless VPC connector, `10.8.0.0/28`)** — used by **both** the planner and the
registration service to reach the private ADS VM (+ relay). Firewall allows the connector range → `ads`
(8888/8999) and → `vllm-relay` (8100).

**Service accounts** — `agent-runtime@` (low-priv identity agents run as: Vertex user),
`registration-service@` (reg-service runtime + deployer: `run.admin`, `serviceAccountUser` on
agent-runtime, `cloudsql.client`, secret access, `vpcaccess.user`). Planner runs as the **default compute
SA** (`267355023116-compute@`, Editor-carrying → hardening follow-up). Captured in
[`scripts/gcp_iam_step7.sh`](../../../deployment/scripts/gcp_iam_step7.sh).

**Secret Manager** — `vllm-api-key` (planner's `OPENAI_API_KEY`), `disaster-card`, `registration-db-dsn`
(shared Cloud SQL DSN), `github-token` (reg-service build-commit path), `credentials-encryption-key`
(planner AES-256-GCM). Note: `registration-db-password` (base64) is **superseded** by the DSN secret.

**ENGR vLLM (`gpt-oss-120b` on `h100-2405`)** — *not a GCP resource*; primary LLM via the relay.

## What's live vs. deferred
- **Live (as of 2026-08-03):** planner (on Cloud SQL) + real ADS + authenticated A2A + on-demand agents + LLM failover, **and
  the full register→build→deploy→publish automation** (registration service + CloudRunBackend + Cloud SQL).
- **Next when cloud work resumes** (paused since the end of summer 2026; cloud is a critical leg of the
  project going forward, so resuming it is a priority — ENGR is the live public deployment meanwhile): the
  **public front door** (decided approach Option A, gateway mints an ID token → run.app; build outline
  in `IMPLEMENTATION_CLOUDRUN_BACKEND.md` Step 10) and the **Option-1 cutover**
  (`agenticweb.engr.wustl.edu` → GCP). Plus the rest of the specialist agent fleet. Handoff summary:
  [`README.md`](README.md#status--deferred-work-handoff).

## Cost / pause
Always-on billers: planner (min=1), **registration (min=1)**, **Cloud SQL `agenticnetwork-db`**, `ads` VM,
`vllm-relay` VM, the connector. To pause the serverless bits: `min-instances=0` on planner + registration;
`gcloud compute instances stop ads vllm-relay`; `gcloud sql instances patch agenticnetwork-db
--activation-policy=NEVER` to stop the DB. The connector can't be paused (delete/recreate for long idles).
