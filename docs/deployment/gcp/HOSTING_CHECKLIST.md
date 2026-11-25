# Checklist: AgenticNetwork fully hosted on GCP

Everything needed to go from today's state to **the whole system served from GCP**:
`agenticweb.engr.wustl.edu` answered by the GCP chat app, every agent on Cloud Run, and the ENGR
services retired. The LLM stays on the ENGR cluster for now (Phase 0), reached through the relay.

Written 2026-11-25 from the repo docs and the 2026-11-16 Cloud Run load test
(`../../../../benchmarks/RESULTS.md` §8–12, in the workspace folder above this repo).
Project `di2-agenticweb`, region `us-central1`.

**Where things stand.** Live on GCP since 2026-08-03 and exercised again on 2026-11-16: the chat app
and registration service on Cloud Run, Cloud SQL, the ADS VM, Vertex fallback, and the
register → build → deploy → publish loop. Not done: the public front door, the cutover, the specialist
agents, the LLM still depends on ENGR, and the Nov 16 test found agent calls failing (404) and Docker
Hub pulls failing.

Each phase ends with a **Done when** line. Phases 0–2 come first; 3–6 can run in parallel; 7–9 are in
order at the end.

---

## Phase 0: Decisions (settle before building)

- [x] **LLM: stay on the ENGR cluster for now (decided 2026-11-25).** ENGR vLLM (`gpt-oss-120b`)
      stays primary through the `vllm-relay` VM, with Vertex as the fallback. Revisit later; Phase 3
      now only hardens the relay path.
- [x] **Agents use `ingress=all`, IAM-only (decided 2026-11-25).** This fixes the Nov 16 404s
      (`RESULTS.md` §12). Agents accept traffic from the internet, but every request still needs a
      Google ID token from an account with `run.invoker` (`--no-allow-unauthenticated`). The chat app
      keeps `private-ranges-only` egress, so no Cloud NAT is needed. Not chosen: routing the chat
      app's egress through the VPC (`--vpc-egress=all-traffic`), which needs Cloud NAT and has a small
      ongoing cost.
- [x] **Images: Artifact Registry (decided 2026-11-25).** All images (infra and custom agents) move
      there. Cloud Run failed to create a revision from a Docker Hub image (`mirror.gcr.io ... not
      found`); copying it to Artifact Registry fixed it. The code changes are in Phase 2.
- [x] **Custom agents on GCP are built with Cloud Build (decided 2026-11-25).** Today, approving an
      agent commits its build context to `washu-dev/agent-directory`, a GitHub Action builds the image to
      Docker Hub, and registration polls Docker Hub. On GCP, registration will instead submit the
      build context straight to Cloud Build, which pushes to Artifact Registry. That removes the GitHub
      token and Docker Hub from the GCP path, and Cloud Build's completion notice wakes registration
      (Phase 2). The cluster keeps the GitHub path until ENGR is retired. Trade-off: approved code is
      no longer recorded in git history, so a versioned Cloud Storage bucket keeps each approved build
      context instead.
- [x] **Front-door credential: SA keys are allowed (checked 2026-11-25).** Both
      `iam.disableServiceAccountKeyCreation` and `iam.managed.disableServiceAccountKeyCreation` are
      `enforce: false` on `di2-agenticweb` (`gcloud org-policies describe <constraint>
      --project=di2-agenticweb --effective`). So the gateway can use an SA key file (Option A as
      written). Workload Identity Federation stays the safer later upgrade.
- [ ] Still open for the front door: which gateway layer proxies today (nginx or Apache), whether
      that host can run `google-auth` or `gcloud`, and who has admin access to it.
- [ ] **Which specialist agents go to GCP**, and whether geospatial and coding are in scope for launch.
      See Phase 5.
- [ ] **Capacity target**: how many people chatting at once at launch. One chat-app instance tops out
      around 50 turns/min, about 30–40 active people (`RESULTS.md` §10).
- [ ] **Budget**: confirm the ~$220–320/mo control-plane estimate plus the LLM choice. Set a billing
      budget with alerts.

**Done when:** each item above has an answer written in `MIGRATION.md` "Decisions".

## Phase 1: Re-verify and reconcile what exists

**Inventory, 2026-11-25** (read-only checks plus three test chat turns):

| Resource | State found |
|---|---|
| `chat-app` | Up, rev `chat-app-00003` (Nov 16), image in Artifact Registry `:5bf4fe0-dirfix`. min 0 / max 1, connector `planner-connector`. **LLM = Vertex primary with no fallback** (switched while vLLM was down on Nov 16). Still `CHAT_APP_ALLOW_ANON=1`. **No `CREDENTIALS_ENCRYPTION_KEY`**: the secret doesn't exist, so saving a user key fails (`chat_app/store/crypto.py` raises). |
| `planner` | Old service still up (Jul 27 image `di2washu/agenticnetwork-planner:phase3`), vLLM primary + Vertex fallback. To delete. |
| `registration` | Up, rev `registration-00004` from **Aug 3**, image `di2washu/...:latest` (Docker Hub). Still `PLANNER_INVOKER_SA` + `REGISTRATION_DB_DSN`. `ADMIN_EMAILS` = one person. Older than `chat-app`, so the chat app's approval view can't approve (`README.md` "Dev workflow"). |
| `disaster` | Up, `ingress=all`, runs as the default Compute SA (not `agent-runtime`). Works from the chat app. |
| `agent-echo-test` | Up, `ingress=internal`, so the chat app gets **404** calling it (reproduced). Keep it for the Phase 2 ingress fix. |
| `ads` VM | Running, `--tmpfs /root/.agntcy/dir` applied, host firewall open on 8888/8999, 2 routed agents. Logs repeat `Failed to get namespace entries for labels` every 5 s for one CID (investigate). |
| `vllm-relay` VM | Running. |
| Cloud SQL | `db-f1-micro`, public IP with no authorized networks (connector only). **Automated backups are off.** |
| Connector | `planner-connector` (10.8.0.0/28), e2-micro, 2–10 instances. |
| Secrets | `disaster-card`, `github-token`, `registration-db-dsn`, `registration-db-password` (superseded), `vllm-api-key`. |
| Firewall | Connector → `ads` 8888/8999 and → `vllm-relay` 8100. `default-allow-rdp` open to `0.0.0.0/0` (no Windows VMs; remove in Phase 4). |

**Changes applied, 2026-11-25:**

- [x] Inventory taken (table above).
- [x] Cloud SQL: daily backups at 08:00 UTC (7 kept) and point-in-time recovery (7 days) turned on,
      plus an on-demand backup before the registration upgrade. The registration logs show a
      database connection error at 10:23 UTC, probably from the instance restarting during this change.
- [x] Secret `credentials-encryption-key` created (32 random bytes, base64), readable by the chat
      app's SA, and mounted on `chat-app` as `CREDENTIALS_ENCRYPTION_KEY` (rev `chat-app-00004`).
      Not exercised yet: with `CHAT_APP_ALLOW_ANON=1`, `/me/credentials` answers 401 without an identity.
- [x] `chat-app` LLM: cluster vLLM was **down** (the relay's `vllm-tunnel` service runs, but
      `localhost:8100` on the relay doesn't answer). Vertex stays primary for now, with vLLM as the fallback
      (`PLANNER_LLM_FALLBACK_*` → `http://10.128.0.2:8100/v1`, `openai/gpt-oss-120b`). When the
      ENGR vLLM job is back, swap them (Phase 3).
- [x] `registration` upgraded: image built in Cloud Build from commit `17afb23` and pushed to Artifact
      Registry (`agenticnetwork-registration:17afb23`). Now uses `CHAT_APP_INVOKER_SA` and `DATABASE_URL`;
      `ADMIN_EMAILS` has two admins (rev `registration-00005`). Both services are now on `DATABASE_URL`.
- [x] `planner` deleted (it had no `run.invoker` grants to move). `planner-connector` kept: both
      services use it, and a connector can't be renamed.
- [x] `ads` container confirmed running with `--tmpfs /root/.agntcy/dir`.
- [x] `DEPLOY_YOUR_OWN.md` step 12 checks pass: `/health` 200, `/ready` all ok, `/agents` lists
      2 agents, and a chat turn through the disaster agent answers.
- [x] Uncommitted work committed on `fix/directory-off-event-loop` (PR).
- [ ] `agent-echo-test` kept on purpose for the Phase 2 ingress fix.
- [x] `DEPLOYED_COMPONENTS.md` rewritten from the inventory (2026-11-25).
- [ ] **Moved to Phase 2.** The register → approve → deploy → discover → answer round trip
      (`IMPLEMENTATION_CLOUDRUN_BACKEND.md` "End-to-end acceptance"). On 2026-11-25 approval failed
      because GitHub answers **401 to `github-token`** (expired or revoked; the secret still has only
      its 2026-08-03 version). With custom-agent builds moving to Cloud Build, the round trip runs on
      that path instead, so the token isn't needed for GCP. A test agent, `Loop Check` (custom echo
      handler, skill 10203), is registered, uploaded and approved, waiting to be re-approved once the
      Cloud Build path is deployed.

**Bugs found by the registration upgrade** (both predate today's changes):

- [x] **Fixed 2026-11-25** (commit `b61cb8b`, rev `registration-00006`; startup logged
      `recovery_complete_cloud confirmed=1 redeployed=0`). Registration also now **scales to zero**
      (`min=0`, `max=1`, CPU allocated while running) instead of running all the time, which saves
      about $45–50/month at list price. Not yet observed: the instance actually reaching zero and cold-starting on the next request.
      **Every registration restart redeploys every live agent.** `CloudRunBackend.get()`
      (`registration_service/backend.py`, used by `is_alive`) looks for a `Ready` condition in the
      Cloud Run v2 `conditions` list. But v2 reports `Ready` as `terminal_condition`, and
      `conditions` only holds `RoutesReady` / `ConfigurationsReady` (checked against the live API). So recovery
      thinks every agent's service is gone: it removes the agent's card from the directory and
      redeploys it. Today that happened to `agent-echo-test`. Fix: read `svc.terminal_condition`, and
      fix the test's fake client to match the real API.
- [x] **Fixed 2026-11-25** (commit `d9aad31`, rev `registration-00007`). The cause was data, not
      the directory: Echo Test was registered with skill **id 1**, the `natural_language_processing`
      category, and registration accepted any id. Registration now answers 400 for ids not in
      `OASF_SKILLS`, and spawn and deploy fail early for older rows with one.
      **The directory now rejects category-level skills.** Republishing Echo Test's card failed:
      `"natural_language_processing" is not a valid option at "skills[0]"; please specify a concrete
      skill within the "base_skill" taxonomy`. Any agent registered with a category-level OASF skill
      can't be published. Registration's preflight (`registration_service/oasf_skills.py`) should
      require a concrete skill. Echo Test still shows in `/agents`, from an older record.

**Done when:** the inventory doc matches reality (done 2026-11-25). The full-loop check moved to Phase 2.

## Phase 2: Fix the bugs found in the Nov 16 testing

- [x] **Done 2026-11-25** (commit `4308b23`). New agents deploy with `ingress=all`, IAM-only.
      `agent-echo-test` switched by hand (`disaster` already was). Checked live: a request with no
      token gets 403, and a chat turn through the echo agent gets its reply (it got 404 before).
      **Agent 404s.** Switch the `CloudRunBackend` to deploy agents with `ingress=all`
      (`registration_service/backend.py`: the `"internal-only"` deploy spec and
      `INGRESS_TRAFFIC_INTERNAL_ONLY`; `registration_service/tests/test_cloudrun_backend.py` asserts
      the old value). Keep `--no-allow-unauthenticated`. Update already-deployed agents with
      `gcloud run services update <agent> --region=us-central1 --ingress=all`, then confirm a request
      without a token gets 403. Re-run the load test with the driver's strict check: echoed text
      returned and `call_agent` completed. Update the deploy text in `DEPLOY_YOUR_OWN.md` step 13
      and its 404 troubleshooting row.
- [ ] **Code done and deployed; waiting on the project Owner.** Built 2026-11-25 (commit `18e5ead`,
      `registration_service/gcp_build.py`, 10 tests); deployed to `registration` with
      `AGENT_BUILDER=cloudbuild`, `AGENT_CODE_BUCKET=di2-agenticweb-agent-code`,
      `CLOUD_BUILD_SERVICE_ACCOUNT=agent-builder@…` and `IMAGE_REGISTRY` = the Artifact Registry repo.
      Created: the versioned bucket, `agent-builder@`, `build-notifier@` (`run.invoker` on registration),
      topic `cloud-builds` and push subscription `registration-build-events`. **Uploads are saved and
      restored from the bucket** (checked live: Loop Check's upload is in `uploads/`). Approval
      still fails: Cloud Build answers 403 until the Owner grants `cloudbuild.builds.editor` to
      `registration-service@` and `logging.logWriter` (project) plus `artifactregistry.writer` (repo
      `agenticnetwork`) to `agent-builder@`. `deployment/scripts/gcp_cloud_build_setup.sh` has all
      three. Then re-approve Loop Check.
      **Build custom agents with Cloud Build** (Phase 0 decision). Code in `registration_service`:
      - On approval with `SPAWN_BACKEND=cloudrun`, assemble the same build context the GitHub commit
        carries today (`builder.generate_dockerfile`, the card, the `build_manifest` `agent.json`, the
        uploaded code, the `agent_skeleton` runtime). Upload it as a tarball to a **versioned** Cloud
        Storage bucket (the record of approved code), and create a Cloud Build build from that object.
        The build pushes `…/agenticnetwork/agenticnetwork-custom-<slug>:<approval id>` to Artifact Registry.
        Store the build id on the agent row (alongside or instead of `build_commit`).
      - Finished builds: Cloud Build publishes to the Pub/Sub topic `cloud-builds`. A push subscription
        (OIDC token from a dedicated SA with `run.invoker` on `registration`) calls a new registration
        endpoint. That endpoint checks the build, records the image digest, marks the agent
        `image_ready` and deploys it. This wakes a scaled-to-zero registration, so a long build no
        longer waits for the next visitor. Keep the 30 s poll as a fallback, asking Cloud Build's API
        (`builds.get`) instead of Docker Hub. A failed build marks the agent `failed`, with the build's log URL.
      - Keep the cluster on the GitHub path (`ClusterBackend`; the `owns_build` flag already separates
        backends). `github-token` is then needed only there.
      - Tests: a fake Cloud Build client, like the fake Cloud Run client.
      - IAM: a build SA with `artifactregistry.writer` and `logging.logWriter`. On `registration-service@`:
        `cloudbuild.builds.editor`, `iam.serviceAccountUser` on the build SA, and `storage.objectCreator` on
        the bucket. Cloud Run pulls the image from Artifact Registry in the same project.
      - Then run the round trip moved from Phase 1: re-approve `Loop Check`, and check the build, the
        deploy by digest, the card in `/agents` and a chat answer. Then delete `agent-echo-test` (and its old
        directory record) through the registration API.
      - Docs: `DEPLOY_YOUR_OWN.md` step 13 (drop the GitHub repo, Docker Hub and public-image requirements
        for Cloud Run), `REGISTRATION_SERVICE.md`, `gcp/README.md` "Dev workflow".
- [x] **Listing cache done 2026-11-25** (`chat_app/directory_cache.py`, commit `bf8360f`;
      `CHAT_APP_DIRECTORY_CACHE_SECONDS=10` on `chat-app`). The network directory client is still open.
      **Directory CPU cost.** Every listing launches `dirctl` once per agent (about 0.45 CPU-s per
      listing with 2 agents, growing with directory size). Add the proposed ~10 s cache for the chat
      app's listing with one fetch in flight (`RESULTS.md` §11). Longer term, replace the `dirctl`
      subprocess with a network/gRPC client (`MIGRATION.md` hardening backlog).
- [x] **Not needed at this size (load test 2026-11-25).** With the listing cache, one instance served
      204 turns/min at 20 users with no 429s, CPU p99 ≤ 17%, Cloud SQL ≤ 9 connections
      (`benchmarks/RESULTS.md` §13). Kept at max 1. More instances would add redundancy, not
      capacity; revisit if a capacity target well above ~150 active people is set (Phase 0). Checked
      first: conversations and pending confirmations live in Postgres (the planner reloads a
      conversation on another instance); only the admin stats and bug-report trace log are per-instance.
      **Scale-out settings.** Chat app runs `max-instances=1`, `concurrency=80`. Set `--concurrency`
      near the measured per-instance limit (~10) so Cloud Run scales before an instance saturates, and
      raise `--max-instances`. First confirm nothing per-user is held only in memory (e.g. the
      in-process `_request_log` used for bug-report traces). `registration` stays a singleton
      (`min=max=1`).
- [x] **Not needed while max-instances is 1** (≤ 9 connections under load; `db-f1-micro` allows about 25).
      **Cloud SQL size.** `db-f1-micro` has a low connection cap, and each chat-app instance opens up
      to ~10 connections. Move to a tier that fits `max-instances × 10` plus registration.
- [x] **Done 2026-11-25** (commit `280e313`, `chat_app.server.probe_targets`). The health loop
      skips `*.run.app` agents unless `CHAT_APP_HEALTH_PROBE_RUN_APP` is set; the old skip list was removed
      from `chat-app`.
      **Scale-to-zero agents.** Add on-demand agents to `CHAT_APP_HEALTH_PROBE_SKIP_AGENTS`, or replace that
      list with an `on_demand` card marker (`IMPLEMENTATION_CLOUDRUN_BACKEND.md` follow-ups). Otherwise the
      per-minute health probe keeps every agent warm and billing.
- [x] Load test re-run 2026-11-25 at 1/5/10/20 users: 97–100% OK, 0 Cloud Run 429s, p95 ≈ 11 s.
      No capacity target was set in Phase 0, so 20 users (about 200 turns/min) stands in for it.
      Re-run `benchmarks/cloudrun_load.py` at the Phase 0 capacity target with 0 Cloud Run 429s.

**Done when:** the load test passes at the target with real agent calls succeeding.

## Phase 3: LLM path (ENGR vLLM primary, Vertex fallback)

Decided 2026-11-25: the LLM stays on the ENGR cluster for now. This phase makes that path dependable.

- [x] **Already true (checked 2026-11-25).** The relay's autossh runs as `vllm-tunnel.service`, enabled with
      `Restart=always`, so it survives a reboot. `tcp:8100` is open only to the connector range from
      outside the VM's subnet (`allow-vllm-relay-from-connector`), plus other VMs on the VPC through
      `default-allow-internal`; nothing opens it to the internet.
- [x] **Installed on `vllm-relay` 2026-11-25** (old unit kept at `/root/vllm-tunnel.service.bak-20261125`).
      Checked against the real cluster: it logged in, ran `squeue`, found no `agenticnetwork-vllm` job
      running, and retries every minute; it connects by itself once a job runs.
      **Keep the relay pointed at the right node.** Found 2026-11-25:
      the vLLM job has `--time=12:00:00`, so it ends after 12 hours and a resubmitted job can land on
      any GPU node, while the relay forwarded to `h100-2405` only (its log shows `Connection refused`
      since 2026-11-16). `deployment/relay/` looks the node up with `squeue` on the ENGR login host
      each time the tunnel starts, and a 2-minute health check restarts the tunnel when vLLM stops
      answering. Tested locally against fake `ssh`/`curl`/`systemctl`; install on the VM with
      `deployment/relay/install.sh` (it runs `squeue` as the ENGR account the tunnel already logs in as).
- [x] **Decided 2026-11-25: as on ENGR today.** The team resubmits `start_vllm.sbatch` the way it already
      does for the cluster deployment; nothing new on the GCP side. The difference on GCP: between jobs the
      chat app stays up on Vertex instead of going down with the cluster deployment.
      **Keep a vLLM job running.** The relay follows the job but doesn't start it. Someone has to submit
      `start_vllm.sbatch` every 12 hours, or the cluster side needs a longer time limit or an automatic
      resubmission (for example `scrontab`, if ENGR allows it). Decide who owns this. Until then, when
      the vLLM job is up, switch `chat-app` back to vLLM primary with Vertex as the fallback (it runs on
      Vertex primary since 2026-11-16).
- [x] **Fallback alert created 2026-11-25.** Log metric `chat_app_llm_failover` (the planner's
      `planner_llm_failover` line) and alert policy "Chat app is using the fallback LLM", which includes a
      runbook note. Notifies an email channel (`m.tahir (WashU email)`, added 2026-11-25). While Vertex is
      the primary the alert stays quiet; it matters once vLLM is the primary again. The chat app's SA already has Vertex access (Editor);
      `aiplatform.user` is needed only for the dedicated SA in Phase 4.
- [ ] Decide what the agents use. The registration service copies `OPENAI_BASE_URL`, `OPENAI_API_KEY`
      and `AGENT_MODEL` into every agent **as plain env vars**, readable by anyone who can view the service.
      Agents on the relay need the connector (`VPC_CONNECTOR` on `registration`, plus
      `roles/vpcaccess.user` for its SA); use a low-value vLLM key.
- [ ] Later, if the LLM moves to GCP: Vertex primary (`DEPLOY_YOUR_OWN.md` step 10, option B) or a GCP
      GPU VM (option C), then retire `vllm-relay`.

**Done when:** a restart of the vLLM job or the relay VM recovers without manual steps, and a vLLM
outage raises an alert while chat keeps working on Vertex.

## Phase 4: Security and IAM hardening

- [ ] **Dedicated chat-app service account: prepared, waiting on the Owner.** Created 2026-11-25:
      `chat-app@`, with `secretAccessor` on `registration-db-dsn`, `credentials-encryption-key` and
      `vllm-api-key`, and `run.invoker` on `registration`, `disaster` and `agent-echo-test`. Still needed,
      project-level (Owner): `roles/cloudsql.client` and `roles/aiplatform.user` for it. Then, in this order:
      `gcloud run services update registration --update-env-vars=CHAT_APP_INVOKER_SA=chat-app@di2-agenticweb.iam.gserviceaccount.com`,
      `gcloud run services update chat-app --service-account=chat-app@di2-agenticweb.iam.gserviceaccount.com`,
      and grant it `run.invoker` on any agent deployed since. Check `/ready` and a chat turn through an agent.
- [x] **`/register/*` pass-through fixed 2026-11-25** (commits `b0a6416` and `6980889`, the
      second for `X-Serverless-Authorization`). It attaches an ID token for `*.run.app` and no longer forwards the
      caller's credentials. `REGISTRATION_SERVICE_URL` is set on `chat-app`, and the Compute SA has `run.invoker`
      on `registration`. Checked live: `/register/agents` through the chat app lists the registered agents.
- [x] **`disaster` runs as `agent-runtime@`** (2026-11-25), from an Artifact Registry copy of its image
      (`agenticnetwork-disaster:vertex`; Cloud Run could not create a revision from the Docker Hub one).
      Still answers through the chat app, on Vertex.
- [x] **`default-allow-rdp` deleted** (2026-11-25): RDP open to `0.0.0.0/0`, with no Windows VMs.
- [x] **`ADMIN_EMAILS`** set on `chat-app` to match `registration` (two admins).
- [x] **`PLANNER_POLICY_MODE=strict`** on `chat-app`: a call to a write skill needs the user's confirmation.
      No GCP agent has write skills yet. **The no-skill gap is fixed** (commit `6b3490f`, deployed): a call
      that names no skill now needs confirmation when the agent advertises a skill whose id or tag names a
      side effect (`email/send`, `code/bug_fix`); read-only agents are unchanged (checked live with
      `disaster` and the echo agent). The card converter also guessed writes from a skill's
      name and description, which gated 7% of disaster-agent turns in the Locust load test; fixed
      (commit `cc97409`, deployed): ids, tags and explicit `read`/`write` tags only. Re-run: 0 of 80.
- [x] **`GITHUB_TOKEN` and `GITHUB_REPO` removed from `registration`** (Cloud Build path). The
      `github-token` secret is still in Secret Manager; delete it once the cluster no longer needs this copy.
- [ ] **Secrets still missing on `chat-app`**, each needing a credential from a person:
      - Google OAuth client ID/secret for the `/auth/google` "connect your Google account" flow (used
        by the email agent, Phase 5): an OAuth client in this project, redirect URI on the front door's hostname.
      - `GITHUB_ISSUES_TOKEN` and `GITHUB_ISSUES_REPO` (a private repo) for the "Report a bug" form; it
        answers 502 without them.
- [ ] **VMs run as the default Compute SA** (`ads`, `vllm-relay`), with narrow access scopes (storage read,
      logging, monitoring, Pub/Sub). Lower risk than Cloud Run. A dedicated VM account needs
      `logging.logWriter` and `monitoring.metricWriter` (Owner), and changing it stops the VM, which takes the
      directory down for a minute.
- [ ] Agent builds are set to run as `agent-builder@` and wait on its Phase 2 grants (or switch them to the
      default Compute SA as a stopgap, which has Editor).
- [x] Cloud Armor / ingress: the services stay `--no-allow-unauthenticated`; the org blocks `allUsers`.

**Done when:** no runtime identity holds Editor, and no secret is a plain env var except the agent LLM
variables you chose to accept in Phase 3.

## Phase 5: Specialist agents on GCP (`agent-directory` repo)

For each agent: image in Artifact Registry → Cloud Run service (`min=0`, as `agent-runtime` or its own
SA, ingress per Phase 0) → card URL = its `run.app` URL → card published to the GCP ADS with a routable
`oasf:` skill → `run.invoker` for the chat app → added to the probe-skip list → a real query through the
chat app. Agents must **not** self-publish/unpublish tied to process lifetime on scale-to-zero
(`DOWNSTREAM_CONSIDERATIONS.md` #1).

- [ ] **disaster_response**: already on Cloud Run since Phase 3 of the migration. Re-verify, move the image to
      Artifact Registry, apply the ingress fix.
- [ ] **email**: Chat Completions already. Mount Google OAuth credentials from Secret Manager (the image
      no longer bakes them). Move per-user refresh tokens off local disk into the DB, since scale-to-zero loses
      local disk. Fill `notice_list.txt` or drop `group_send`.
- [ ] **intelligence**: env-only LLM change. `BRAVE_API_KEY` → Secret Manager. Remove the `dirctl`
      self-publish in `_vendor_v2/`. Fix its stale card.
- [ ] **coding**: env-only LLM change, plus inject `api_base`/`api_key` into mini-swe-agent and set
      `MSWEA_COST_TRACKING=ignore_errors`. **Isolation:** it runs bash unconfirmed. Minimum:
      `--concurrency=1`, its own SA with no permissions, no VPC connector, and verified scratch-dir cleanup.
      Long requests need a matching Cloud Run `--timeout`.
- [ ] **scout**: it uses a different directory (gRPC `AgentDirectory` on `:8889`, not `dirctl` on
      `:8888`). Point it at the GCP ADS and verify the others can discover it, or launch without scout.
      `BRAVE_API_KEY` → Secret Manager. Fix `scripts/call_scout.py`.
- [ ] **geospatial**: on the Responses API plus Gemini, with vision and web-grounding needs. On GCP the
      simplest path may be **Gemini on Vertex** for both pipelines rather than a vLLM rewrite. Decide
      first. `GOOGLE_API_KEY` / `GOOGLE_MAPS_API_KEY` → Secret Manager. Its `attack_bundle` is a
      re-identification tool, so get sign-off before exposing it publicly.
- [ ] **Registered (hackathon) agents** in `registered_agents/` (research-atlas, claimanchor,
      citeguard, paper-reviewer, geoagent, arvixagent, …): redeploy each through the registration
      pipeline, or decide which to drop. Only uploaded-code (`custom`) agents work on the Cloud Run
      backend. `llm` and `proxy` agents are rejected there, so either convert them or add backend support.
- [ ] Optionally fix the card metadata loss on registration (70% of index terms lost, `RESULTS.md` §2),
      which hurts routing as the directory grows.

**Done when:** `GET /agents` on the GCP chat app lists every in-scope agent, and each answers a real query.

## Phase 6: CI/CD, reproducibility, operations

- [ ] **CI builds: config ready, trigger needs a GitHub org admin.** `deployment/cloudbuild-ci.yaml` builds
      `chat-app`, `registration` and `ads` in parallel to Artifact Registry as `:<short sha>` and `:latest`
      (run by hand 2026-11-25: 5m23s, success). A trigger on merges to `main` needs the Google Cloud Build
      GitHub App installed on `washu-dev` and the repo connected (global region); the command is in
      `gcp/README.md` "Automatic image builds".
- [x] **Deploy by commit tag, not `:latest`** — every deploy since 2026-11-25 uses `:<short sha>`
      (`gcloud run services update <svc> --image=…:<sha>`).
- [x] **Rollout order** (`registration` before `chat-app`) is in `gcp/README.md` "Dev workflow" and in the
      CI config's header.
- [ ] Optional: infrastructure as code (Terraform), or one script, so the project can be rebuilt.
      `DEPLOY_YOUR_OWN.md` plus `gcp_iam_step7.sh` and `gcp_cloud_build_setup.sh` are the closest today.
- [x] **Backups (2026-11-25).** Cloud SQL daily backups and point-in-time recovery (Phase 1). `ads` disk:
      snapshot schedule `ads-daily` (09:00 UTC, 7 days) plus a manual `ads-manual-20261125`. Restoring
      stored credentials needs the `credentials-encryption-key` secret.
- [x] **Artifact Registry cleanup policy (2026-11-25):** delete a version only when older than 30 days and
      not among its image's 10 most recent; keep `latest` and `vertex` tags.
- [x] **Alerts (2026-11-25), emailing `m.tahir (WashU email)`:** server errors (`chat-app`, `registration`);
      Cloud Run 429s on `chat-app`; directory unreachable (new `directory_unavailable` log line, commit
      `4cbd026`); agent build/deploy/upload/recovery failures; `ads` VM not running; chat app on the
      fallback LLM (Phase 3). No uptime check on the chat app on purpose: it would keep the scale-to-zero
      instance awake. Revisit with min-instances 1 at launch, and add the front-door token refresh in Phase 7.
- [ ] **Billing budget alerts** need Billing Account Costs Manager on the billing account (not the project).
- [x] Runbooks updated as each phase landed (`gcp/README.md`, `DEPLOYED_COMPONENTS.md`, this checklist).

**Done when:** a merge to `main` produces a deployable image without anyone's laptop (needs the trigger),
and someone gets an email if prod goes down (done).

## Phase 7: Public front door (Option A; `IMPLEMENTATION_CLOUDRUN_BACKEND.md` Step 10)

- [ ] Create SA `agenticweb-gateway@` with `run.invoker` on **`chat-app` only**, and one key for it
      (keys are allowed, Phase 0). Grant nothing else to that SA, so a leaked key can only call the chat app.
      Set a rotation date (e.g. every 90 days: create a new key, deploy it, then delete the old one), and
      never commit the key or copy it anywhere but the gateway.
- [ ] On the WashU gateway host (out of repo): install the credential (chmod 600, dedicated user), and
      a systemd timer (~every 30 min) that mints an ID token with audience = the chat-app `run.app` URL.
- [ ] Gateway proxy config, staged on a **scratch hostname** first: upstream = `run.app`. It must add
      `Authorization: Bearer <token>`, set SNI and `Host` to the `run.app` host, keep
      `proxy_buffering off` for `/messages/stream`, forward the mod_shib identity headers, and
      **strip client-supplied `mail` / `eppn` / `remote-user`**.
- [ ] Raise the gateway's read timeout for SSE and cold starts.
- [ ] Validate from the gateway: `/health` with a token → 200; without a token → 403; a forged `mail`
      header from a browser is stripped; a real SAML login reaches the chat app with the right identity;
      saved conversations, stored keys and the Google OAuth connect flow work; registering an agent through the
      chat app works (needs the Phase 4 pass-through fix). Confirm the token refresh survives an expiry.
- [ ] Remove `CHAT_APP_ALLOW_ANON` and `PLANNER_ALLOW_ANON` from `chat-app`, **in the same change** as the
      upstream flip, so there is never an anonymous public path.

**Done when:** the scratch hostname serves the GCP chat app with real WashU login, and every check above passes.

## Phase 8: Cutover (`DOWNSTREAM_CONSIDERATIONS.md` #4)

- [ ] Data: decide whether users, credentials and conversations from the ENGR Postgres move to Cloud SQL
      (export and import, same `CREDENTIALS_ENCRYPTION_KEY`), or whether GCP starts fresh. Tell users either way.
- [ ] Announce a window. Keep the ENGR Slurm job running as the rollback target.
- [ ] Repoint `agenticweb.engr.wustl.edu`'s gateway upstream to the GCP chat app (a config change, not DNS).
- [ ] Smoke test in production: login, a chat turn per agent, registration, the bug-report form.
- [ ] Watch the alerts for the first days. Rollback = flip the upstream back to ENGR.

**Done when:** the public hostname serves GCP with no rollback for an agreed soak period (e.g. two weeks).

## Phase 9: Decommission ENGR

- [ ] Stop the ENGR Slurm job `submit.sbatch`. Keep `start_vllm.sbatch` running: the LLM stays on ENGR (Phase 3).
- [ ] Revoke ENGR-side credentials and tokens that are no longer needed.
- [ ] Mark Tier D as retired in `AGENTS.md` and `docs/deployment/engr/README.md`. Make GCP the documented live deployment.
- [ ] Review the first full month's bill against the estimate. Tune `min-instances`, the SQL tier, and
      the probe-skip list.

---

### Later (not required for "fully hosted")

- Option 2 / IAP + Workforce Identity Federation to retire the campus Shibboleth box.
- A `GKEBackend` for GPU agents and per-request sandboxes (coding).
- Per-agent service accounts instead of the shared `agent-runtime`.
- Real A2A auth (`securitySchemes`) on agent cards.
- Unifying the duplicated protocol code (`_vendor_v2/`, scout's own stack).
