# GCP Deployment

Running AgenticNetwork on **Google Cloud** (project `di2-agenticweb`, region `us-central1`). This is
the cloud deployment, brought up **additively** alongside the [ENGR cluster](../engr/README.md), which
stays the live public deployment. Read side-by-side with the cluster runbook to see how the same system
maps onto each platform.

**Deploying your own instance?** Follow [DEPLOY_YOUR_OWN.md](DEPLOY_YOUR_OWN.md), a parameterized version of this deployment; this page describes the WashU instance.

> **Status (2026-10-13): paused since the end of summer 2026.** Running on cloud is a critical leg of the
> project going forward, so resuming this work is a priority. As of 2026-08-03 the platform and the
> register→deploy automation were live and proven; re-verify the inventory before relying on it. Next: the
> public front door and DNS cutover (scoped below in [Status & deferred work](#status--deferred-work-handoff)).

- **Inventory (as of 2026-08-03) + control commands:** [DEPLOYED_COMPONENTS.md](DEPLOYED_COMPONENTS.md).
- **Design / history / status:** [MIGRATION.md](MIGRATION.md).
- **The register→deploy automation:** [DESIGN_CLOUDRUN_BACKEND.md](DESIGN_CLOUDRUN_BACKEND.md) · [IMPLEMENTATION_CLOUDRUN_BACKEND.md](IMPLEMENTATION_CLOUDRUN_BACKEND.md).

## Architecture

A message hits the chat app (Cloud Run service `chat-app`), whose planner **discovers** agents from ADS, **routes** to the chosen agent's Cloud Run
URL (authenticated with a Google ID token), reasons via **vLLM-primary / Vertex-fallback**, and persists
to **Cloud SQL**. Separately, registering a custom agent triggers a CI build and the **`CloudRunBackend`**
auto-deploys + auto-publishes it — no operator step. The request-path diagram is kept in one place —
[DEPLOYED_COMPONENTS.md](DEPLOYED_COMPONENTS.md) — rather than duplicated here.

## Services → GCP primitives

At a glance (exact names, env, and control commands are in [DEPLOYED_COMPONENTS.md](DEPLOYED_COMPONENTS.md)):

- **Cloud Run** — `chat-app` (the chat app, min=1; named `planner` until 2026-10-25), `registration`
  (singleton min=max=1, `SPAWN_BACKEND=cloudrun`), and on-demand agents (`disaster`, custom agents; min=0).
- **Cloud SQL** — `agenticnetwork-db` (Postgres 15). One `registration` database **shared** by the chat app
  and the registration service (disjoint tables).
- **GCE VMs** — `ads` (the `dirctl` directory, COS VM at `10.128.0.3`) and `vllm-relay` (autossh tunnel to
  the ENGR vLLM).
- **Serverless VPC connector** — `chat-app-connector` (`planner-connector` in the 2026-08 deployment), so
  Cloud Run can reach the private ADS VM.
- **Secret Manager** — LLM key, DB DSN, GitHub token, credential-encryption key, agent cards.
- **Service accounts** — `agent-runtime` (agents run as this), `registration-service` (the deployer);
  see [`deployment/scripts/gcp_iam_step7.sh`](../../../deployment/scripts/gcp_iam_step7.sh).

## How it differs from the ENGR cluster

| Concern | ENGR cluster ([runbook](../engr/README.md)) | GCP |
|---|---|---|
| Orchestration | one Slurm job (`submit.sbatch`); services as parallel `srun --overlap` steps | managed Cloud Run services (one per service) |
| Custom-agent spawn | host loop `srun`s image-ready agents (`ClusterBackend`) | **`CloudRunBackend`** calls the Cloud Run Admin API directly (no host loop) |
| Scaling | every container up for the whole job | Cloud Run autoscale; on-demand agents `min=0` (scale to zero, ~$0 idle) |
| Postgres | one container on `localhost:5432` (`Dockerfile.postgres`) | **Cloud SQL**, reached via `--add-cloudsql-instances` unix socket |
| Networking | host networking on one node | VPC + the `chat-app-connector` to reach the private ADS VM |
| ADS | `dirctl daemon` container | `dirctl daemon` on a COS **GCE VM** |
| LLM | vLLM on a GPU node in the same allocation | vLLM via the `vllm-relay` VM (primary) → **Vertex fallback** |
| Config / secrets | env from `.env`/`secrets.env` via `--export=ALL` | Cloud Run env + **Secret Manager** |
| Images | `di2washu/agenticnetwork-*` | **same images** (shared; the entrypoint branches on `SKIP_STARTUP_WAITS`) |
| Ports | fixed (8090/8091/8888/19105-8) | Cloud Run injects `$PORT` (8080); agents use internal ingress |
| Lifetime | ephemeral (Slurm time limit; node hostname changes each run) | persistent (stable `*.run.app` URLs) |
| Front door | Shibboleth gateway → chat app :8090 | **not yet** — currently anon + proxy (see below); Option-A front door planned |

## How to access

The GCP services are **IAM-private** (no public URL yet) — reached through Google's authenticated proxy,
not the internet. You need `gcloud` authed as a user with `run.invoker` on the service (`run.admin` covers it).

**Chat app (service `chat-app`; `planner` until the migration above):**
```bash
gcloud run services proxy chat-app --project=di2-agenticweb --region=us-central1 --port=8080
```
Then open http://localhost:8080/app (anon is allowed for now — `PLANNER_ALLOW_ANON=1`, the old name
of `CHAT_APP_ALLOW_ANON`).

**Registration service:**
```bash
gcloud run services proxy registration --project=di2-agenticweb --region=us-central1 --port=8091
```
Open http://localhost:8091/. Admin actions need the identity header, e.g.
`curl -X POST http://localhost:8091/agents/<id>/approve -H 'mail: <you>@wustl.edu'` (must be in `ADMIN_EMAILS`).
The proxy adds no identity, so register and upload with curl too, sending `-H 'mail: <you>@wustl.edu'`:
that header records the agent's owner, and only the owner or an admin may upload, restart or delete
it. An agent registered through the UI here has no owner, so its upload is refused
([DEPLOY_YOUR_OWN.md step 13](DEPLOY_YOUR_OWN.md#13-add-agents)).

The **agents** aren't accessed directly — the chat app's planner reaches them internally over the VPC. To grant a
teammate access: `gcloud run services add-iam-policy-binding chat-app --region=us-central1
--member="user:them@wustl.edu" --role="roles/run.invoker"`, and they run the same proxy.

## Dev workflow

- **Local tests, no cloud:** the registration service runs fully in-memory with `FAKE_DB=1`
  (`registration_service/.venv/bin/python -m pytest registration_service/tests -q`; conftest sets `FAKE_DB=1`).
  The `CloudRunBackend` is unit-tested against a fake Cloud Run client — no `google-cloud-run` install, no GCP.
- **Build + push an infra image:** `bash deployment/build_and_push.sh registration` (same script/images as the
  cluster). Redeploy with `gcloud run services update <svc> --image=di2washu/agenticnetwork-<svc>:latest`.
- **Old `planner` service.** Until 2026-10-25 the chat app ran as Cloud Run service `planner`, image
  `agenticnetwork-planner`, connector `planner-connector`, with a start command
  (`planner_agent.scripts.run_server`) that newer images no longer contain; the commands and names on
  this page are the renamed ones. If the old service still exists in `di2-agenticweb`:
  1. Push the renamed image (`bash deployment/build_and_push.sh chat-app registration`).
  2. On `registration`, replace `PLANNER_INVOKER_SA` with `CHAT_APP_INVOKER_SA` (same value) in the same
     update that rolls out the new image. It reads only the new name, and without it agents deployed
     afterwards don't let the chat app call them (it logs `cloudrun_no_invoker_sa`).
  3. Deploy `chat-app` ([DEPLOY_YOUR_OWN.md](DEPLOY_YOUR_OWN.md) step 10, which starts
     `python -m chat_app`). A connector can't be renamed: pass `--vpc-connector=planner-connector`, or
     create `chat-app-connector` (step 4) and delete the old one afterwards.
  4. Grant `run.invoker` on `chat-app` to whoever had it on `planner`, then
     `gcloud run services delete planner --region=us-central1`.
- **Update `registration` before `chat-app`.** The chat app's approval view needs the code revision
  that the registration service reports since the 2026-10-21 approval fixes; in front of an older
  `registration` revision it says so and cannot approve. A config-only `chat-app` update also pulls
  the shared `:latest` again, so push and roll out `registration` first.
- **The register→deploy loop:** register a `custom` agent → approve (fires a commit to `agent-directory`) →
  the `build-custom-agent.yml` GitHub Action builds the image → the reg service's build poll deploys it via
  the `CloudRunBackend`. Full trace + acceptance test: [IMPLEMENTATION_CLOUDRUN_BACKEND.md](IMPLEMENTATION_CLOUDRUN_BACKEND.md).
- **Reach Cloud SQL directly:** `gcloud components install cloud-sql-proxy` then
  `gcloud sql connect agenticnetwork-db --user=regsvc --database=registration` (password is in the
  `registration-db-dsn` secret).

**Operational gotchas** (learned the hard way — see MIGRATION.md / IMPLEMENTATION Step 9 for detail):
- The build poll waits for the image tagged with the approval's commit SHA (the row's `build_commit`); an
  approved agent stuck in `building` means that tag was never pushed (the workflow lacks it, or the build
  failed). Only a row approved before the service recorded commits uses the old **freshness guard**, which
  ignores a `:latest` older than the agent's `updated_at`.
- An **`update_updated_at` trigger** force-sets `updated_at=now()`, so you can't backdate a row; to force a
  redeploy, set `status`/`image_ref` directly and restart (recovery deploys it).
- The startup purge deletes `stopped`/`failed` rows (it spares `failed`+`image_ref` so recovery can retry, and
  any row that still holds an `ads_cid`, so its card is removed first).
- **asyncpg returns JSONB columns as strings** — deserialize (`json.loads`) before use; `FAKE_DB` returns dicts,
  so tests can miss this class of bug.

## Operate / cost / pause

Always-on billers: `chat-app` + `registration` (min=1), Cloud SQL, the `ads` + `vllm-relay` VMs, the connector.
Pause commands are in [DEPLOYED_COMPONENTS.md](DEPLOYED_COMPONENTS.md#cost--pause).

## Status & deferred work (handoff)

**Where this landed (end of summer 2026).** The GCP migration reached a stable, self-contained milestone:
the chat app (then `planner`) + registration service ran on Cloud Run against Cloud SQL, and the full
register→build→deploy→publish automation (`CloudRunBackend`) was **live and proven end-to-end**
(2026-08-03), reachable by operators via the proxy (see [How to access](#how-to-access)). The **ENGR
cluster remains the live public deployment** (`agenticweb.engr.wustl.edu`) — the GCP front-door cutover
was **intentionally paused**, not blocked.

**Next steps when cloud work resumes** (scoped, with the design settled so a successor can execute
directly — none of this blocks using or developing the GCP stack as-is):

1. **Public front door.** The chat app is anon + proxy-only; it isn't exposed to end users yet. Decided
   approach: **Option A** — the WashU Shibboleth gateway mints a Google ID token to reach the private
   chat app (and drops anon at cutover). The build outline is in the CloudRunBackend
   implementation doc's [Step 10](IMPLEMENTATION_CLOUDRUN_BACKEND.md); the decision + rejected
   alternatives (LB+Cloud Armor / IAP+WIF) are in [DOWNSTREAM_CONSIDERATIONS.md](../DOWNSTREAM_CONSIDERATIONS.md) #4.
   First replace the old `planner` service with `chat-app`, if that is not done yet
   ([Dev workflow](#dev-workflow)).
2. **Option-1 cutover** — repoint `agenticweb.engr.wustl.edu` at the GCP chat app (a reversible config
   change on the out-of-repo WashU gateway). Only after (1).
3. **The rest of the specialist agent fleet** on GCP (email / coding / intelligence / geospatial / scout) —
   each still needs its own per-agent LLM work; orthogonal to the platform.
