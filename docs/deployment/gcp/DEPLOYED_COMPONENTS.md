# Deployed components — what is live on GCP (updated 2026-11-25)

Operator reference for what runs in project `di2-agenticweb`, region `us-central1`. Checked
2026-11-25 with `gcloud run services list`, `gcloud compute instances list`, `gcloud sql instances
list`, `gcloud secrets list` and a chat turn through the proxy; re-check before relying on it. The
plan for the rest of the move is [`HOSTING_CHECKLIST.md`](HOSTING_CHECKLIST.md). Design and history:
[`MIGRATION.md`](MIGRATION.md), [`IMPLEMENTATION_CLOUDRUN_BACKEND.md`](IMPLEMENTATION_CLOUDRUN_BACKEND.md).

Until 2026-10-25 the chat app ran as Cloud Run service `planner` (image `agenticnetwork-planner`).
That service was deleted on 2026-11-25. The VPC connector keeps its old name, `planner-connector`,
because a connector can't be renamed.

## The request paths

```
User path:
  you ─(gcloud run services proxy, for now)─▶ Cloud Run: chat-app ──dirctl──▶ ADS VM (10.128.0.3:8888)  [discover]
                        │                                                    [route ↓ via ID token]
                        │  LLM (Vertex primary → vLLM fallback, for now)    Cloud Run agents (min=0, e.g. disaster,
                        ▼                                                    agent-echo-test) cold-start on demand
        Vertex AI  /  vllm-relay VM :8100 ─autossh─▶ ENGR vLLM              Cloud SQL (agenticnetwork-db) ◀─ chat-app
                                                                            persists users/creds/conversations

Onboarding path (automated, Cloud Build since 2026-11-25):
  register + upload ▶ Cloud Run: registration ─context to bucket─▶ Cloud Build ─▶ Artifact Registry
                        │                                              │ (cloud-builds topic → /internal/build-events)
                        └─ build done ─▶ CloudRunBackend deploys agent by digest (min=0, as agent-runtime SA,
                           ingress=all + IAM, grants chat-app run.invoker) ─▶ publishes card to ADS
```

## Components

**Cloud Run `chat-app`** — the chat app with the planner. Image
`us-central1-docker.pkg.dev/di2-agenticweb/agenticnetwork/agenticnetwork-chat-app:6b3490f`, started
with `python -m chat_app`. `REGISTRATION_SERVICE_URL` points at `registration` (the `/register/*`
pass-through authenticates with an ID token), `ADMIN_EMAILS` (two admins), `PLANNER_POLICY_MODE=strict`. `CHAT_APP_DIRECTORY_CACHE_SECONDS=10`; health probes skip `*.run.app` agents.
One instance served 204 turns/min at 20 users on 2026-11-25 (`benchmarks/RESULTS.md` §13). min 0, max 1, 1 vCPU, 1 GiB,
CPU always allocated while running. Runs as the default Compute SA (`267355023116-compute@`, Editor).
Its replacement, `chat-app@`, is created and granted its secrets and invoker rights; the switch waits on
the Owner's `cloudsql.client` and `aiplatform.user` grants (checklist Phase 4). `ingress=all` + `--no-allow-unauthenticated`
(IAM-private; reach it with `gcloud run services proxy`). `CHAT_APP_ALLOW_ANON=1` until the front door
exists. Connector `planner-connector`, egress `private-ranges-only`, Cloud SQL attached.
- LLM: **Vertex `google/gemini-2.5-flash` primary**, vLLM (`openai/gpt-oss-120b` via the relay) as
  fallback. The intended order is the reverse: switch back once the ENGR vLLM job is up (it was down on
  2026-11-16 and 2026-11-25).
- Secrets: `DATABASE_URL` ← `registration-db-dsn`, `CREDENTIALS_ENCRYPTION_KEY` ←
  `credentials-encryption-key` (created 2026-11-25), `OPENAI_API_KEY` ← `vllm-api-key`.

**Cloud Run `registration`** — the registration service and
`CloudRunBackend` host. Image `.../agenticnetwork/agenticnetwork-registration:18e5ead` (Artifact
Registry). **min 0, max 1** since 2026-11-25: it scales to zero between registrations, and a request
wakes it. CPU stays allocated while it runs, so the build poll (every 30 s) runs for the ~15 minutes
Cloud Run keeps an idle instance. Runs as `registration-service@`. `SPAWN_BACKEND=cloudrun`,
`CHAT_APP_INVOKER_SA` = the chat app's SA, `ADMIN_EMAILS` (two admins), `SKIP_STARTUP_WAITS=1`.
**Custom agents build with Cloud Build** (`AGENT_BUILDER=cloudbuild`): uploads and build contexts in
bucket `di2-agenticweb-agent-code` (versioned), builds run as `agent-builder@`, images pushed to
`IMAGE_REGISTRY` = `us-central1-docker.pkg.dev/di2-agenticweb/agenticnetwork`. Cloud Build's
`cloud-builds` topic pushes to `/internal/build-events` (subscription `registration-build-events`, as
`build-notifier@`). **Approvals fail with 403 until the project Owner runs the three grants in
`deployment/scripts/gcp_cloud_build_setup.sh`.** `GITHUB_REPO`/`GITHUB_TOKEN` were removed 2026-11-25.
Secrets: `DATABASE_URL` ← `registration-db-dsn`, `GITHUB_TOKEN` ← `github-token`.
- **`github-token` is rejected by GitHub (401) as of 2026-11-25**, so approving a custom agent fails
  until the secret has a new token.

**Cloud Run agents (on-demand, min 0)**
- `disaster` — manual deploy from the July work; image copied to Artifact Registry
  (`agenticnetwork-disaster:vertex`) on 2026-11-25. Vertex Gemini, `ingress=all`, runs as
  `agent-runtime@`. Answers through the chat app.
- `agent-echo-test` — deployed by the `CloudRunBackend` in August; switched to `ingress=all` on
  2026-11-25 (the chat app reaches it). Registered with skill id 1 (a category), so the directory
  refuses its card; it is listed from an older record.
- Registered, not built: `Loop Check` (an echo agent for the register-to-answer test), waiting on the
  Cloud Build grants below.

**Cloud SQL `agenticnetwork-db`** — POSTGRES_15, `db-f1-micro`, zonal. Database `registration` is
shared by the chat app and the registration service (separate tables). User `regsvc`; the socket DSN is
in `registration-db-dsn`. Public IP with no authorized networks, so it is reached only through Cloud
Run's Cloud SQL connector. **Backups since 2026-11-25:** daily at 08:00 UTC (7 kept) and point-in-time
recovery (7 days).

**`ads` VM** — e2-small COS VM (`10.128.0.3`): `dirctl daemon` with `--tmpfs /root/.agntcy/dir` and a
co-located `registry:2` (host networking, sqlite, `ads_daemon.gcp.yaml`). A startup script opens
8888/8999 in the COS host firewall. Validates records against `https://schema.oasf.outshift.com`, which
refuses category-level skills. Logs repeat `Failed to get namespace entries for labels` every 5 s for
one record (not yet investigated).

**`vllm-relay` VM** — e2-micro (`10.128.0.2`). `vllm-tunnel.service` forwards `:8100` to whichever ENGR
node runs the `agenticnetwork-vllm` Slurm job (looked up with `squeue` on the login host, as the ENGR
account the tunnel logs in with), and `vllm-tunnel-check.timer` reconnects when vLLM stops answering
(`deployment/relay/`, installed 2026-11-25). No vLLM job was running on 2026-11-25. Alert policy "Chat
app is using the fallback LLM" (log metric `chat_app_llm_failover`) emails when the chat app fails over.

**`planner-connector`** (Serverless VPC Access, `10.8.0.0/28`, e2-micro, 2–10 instances) — used by
`chat-app`, `registration` and `disaster`. Firewall: connector → `ads` (8888/8999) and → `vllm-relay`
(8100); SSH only from WashU (`128.252.0.0/16`); `default-allow-rdp` deleted 2026-11-25.

**Artifact Registry `agenticnetwork`** (docker, `us-central1`) — infra images, built with
`deployment/cloudbuild.yaml` and tagged with the commit SHA.

**Service accounts** — `agent-runtime@` (agents, `disaster` included, run as this; Vertex user), `registration-service@`
(runtime and deployer: `run.admin`, `serviceAccountUser` on `agent-runtime`, `cloudsql.client`, secret
access, `vpcaccess.user`), `chat-app@` (prepared for the chat app, not yet in use), `agent-builder@` and
`build-notifier@` (Cloud Build path), and the default Compute SA for `chat-app`, the two VMs and builds. Grants are in
[`deployment/scripts/gcp_iam_step7.sh`](../../../deployment/scripts/gcp_iam_step7.sh).

**Secret Manager** — `registration-db-dsn`, `credentials-encryption-key`, `github-token` (needs a new
token), `vllm-api-key`, `disaster-card`, and `registration-db-password` (superseded by the DSN secret).

**ENGR vLLM (`gpt-oss-120b`)** — not a GCP resource. Intended primary LLM, reached through the relay.

## Cost / pause

Always-on billers: Cloud SQL, the `ads` and `vllm-relay` VMs, and the connector (min 2 instances).
`chat-app` and `registration` run at min 0, so they bill while serving and while an idle instance
lingers. To pause the rest: `gcloud compute instances stop ads vllm-relay --zone=us-central1-a` and
`gcloud sql instances patch agenticnetwork-db --activation-policy=NEVER` (restart both before use). The
connector can't be paused; delete and recreate it for long idles.
