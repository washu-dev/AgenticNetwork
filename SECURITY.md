# Security policy

AgenticNetwork is research software. This file says how to report a vulnerability, which
versions get fixes, how the system is secured, and the hazards that are known today.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability reporting: on this
repository's **Security** tab, choose **Report a vulnerability**. This opens a draft security
advisory that only the maintainers can see.

- Do not open a public issue, pull request or discussion about a suspected vulnerability.
- Include the affected component and commit, steps to reproduce, the impact, and a suggested fix
  if you have one. Use test data only; never send real credentials or personal data.
- If the Security tab does not offer private reporting, open a public issue that asks a
  maintainer to contact you, without any details of the vulnerability.

Hazards already listed in §2 below are known; reports that show a new way to exploit one are
still welcome.

## Supported versions

There are no releases yet. Only the `main` branch is supported, and fixes land there.

---

## 1. Security model

This describes the system as it runs **today** in the WashU deployment (single trusted Slurm node,
behind a WashU Shibboleth gateway). This is a **research/demo** posture — several things that are
fine on a trusted single node become hard requirements before any broader exposure. The GCP Cloud
Run deployment (on hold since summer 2026; cloud is a critical leg going forward) and the local
dev stack have their own rows in the table below. Companion docs: repo-root
[`CLAUDE.md`](CLAUDE.md) §8–§9 (auth + caveats), [`docs/AUTH.md`](docs/AUTH.md) (the auth model)
and [`docs/deployment/DOWNSTREAM_CONSIDERATIONS.md`](docs/deployment/DOWNSTREAM_CONSIDERATIONS.md)
#2–#3 (the at-scale hardening plan). Update this file when the posture changes.

- **Identity at the front door.** Users sign in at one authenticated gateway, which injects
  identity headers (`eppn`/`mail`/`remote-user`) that the chat app trusts (`chat_app/auth.py`).
  The chat app has no authentication of its own, so it must be reachable only through the gateway.
  It fails closed (401) without an identity when a DB is configured, and a login identity always
  wins over a request-body `user_id`.
- **Admin allowlist.** Admin rights come from the signed-in identity: emails in `ADMIN_EMAILS`
  (`require_admin`: 401 without an identity, 403 if not listed; an empty list means no admins).
  The chat app and the registration service both enforce it.
- **Credentials: encrypted at rest, scoped per agent.** Per-user API keys and OAuth tokens are
  AES-256-GCM encrypted (`chat_app/store/crypto.py`, `CREDENTIALS_ENCRYPTION_KEY`). At request time
  the chat app decrypts them and the planner passes the target agent only the types it declared
  and the user granted, in the A2A task metadata, never through the LLM ([`docs/AUTH.md`](docs/AUTH.md) §4).
  The exceptions, and what happens to the credentials after that hop, are in §2.
- **Network isolation.** Everything behind the gateway trusts everything else. Specialist agents,
  the directory (ADS), vLLM and the registration service's `/internal/*` routes have no
  authentication, so they must not be reachable from outside the node or private network.

### What is protected, and what isn't

| Surface | Auth today | Notes |
|---|---|---|
| Public URL `agenticweb.engr.wustl.edu` | **WashU Shibboleth SSO** (Apache `mod_shib`) | The one authenticated front door. TLS at nginx. |
| Chat app `:8090` | **None of its own** — trusts Apache-injected `eppn`/`mail`/`remote-user` headers (`chat_app/auth.py`), fail-closed (401) when a DB is configured | Safe **only** behind the Shib proxy. Reached raw (or via an SSH tunnel) with a spoofed identity header, auth is bypassed. |
| Chat app admin (`/admin/*`) | **SSO identity ∈ `ADMIN_EMAILS`** (`require_admin`); fail-closed (empty list → no admins) | The shared `X-Admin-Token`/`MAIN_AGENT_ADMIN_TOKEN` + `POST /admin/login` are retired. See [`docs/AUTH.md`](docs/AUTH.md) §2. |
| Specialist ports (9103, 9104, 19105–19108) | **None** — empty `securitySchemes`, bind `0.0.0.0` | Any process on the node's localhost can drive Gmail send, calendar/file writes, code execution, geolocation **directly**, skipping the planner's policy. |
| Registration service `:8091` | Admin endpoints fail-closed on `ADMIN_EMAILS` (dev-open under `FAKE_DB=1`); `/internal/*` has **no access check in code** | Approval is the trust boundary for uploaded code. `/internal/*` relies on `:8091` not being reachable from outside; the chat app's `/register` pass-through refuses it (see §2). Agent responses show configs with secret values redacted (`server.py::_public_config`). |
| ADS `:8888` / vLLM `:8100` | **None** | Trusted-LAN assumption. |
| GCP (Cloud Run, on hold) | **IAM-private**: callers need `run.invoker` (operators use `gcloud run services proxy`) | No SSO front door yet; the chat app (Cloud Run service `planner`) runs `PLANNER_ALLOW_ANON=1` (the old name of `CHAT_APP_ALLOW_ANON`), so per-user identity is not enforced. Agents use internal ingress and are called with Google ID tokens. |
| Dev stack (`scripts/stack`) | A local proxy on `127.0.0.1:8080` drops client identity headers and injects `mail: dev@example.test` | Every service and spawned agent binds `127.0.0.1` (`REGISTRATION_SERVICE_HOST`, `AGENT_BIND_HOST`). For one developer's machine only; never expose it. |
| Docker Compose (`deployment/compose/`) | The dev identity proxy (as above) on `127.0.0.1:18080`, unless the operator puts an authenticating proxy in its place ([`docs/deployment/COMPOSE.md`](docs/deployment/COMPOSE.md)) | Only the front door is published. Agents run as subprocesses of `registration` (see the unapproved-agents hazard in §2), so every signed-in user must be trusted. |

**Takeaway:** the security model is *"one authenticated gateway in front of a fully
trusted node."* Everything inside the node trusts everything else.

---

## 2. Active hazards (true today)

- **Uploaded custom code is not sandboxed by the validator.** `registration_service/validator.py`
  is **pure-AST and never executes** code; `subprocess`/`eval`/`os.system` only produce
  *advisory warnings*, not blocks. The real trust boundary is **admin approval +
  container isolation** — not the validator. Only the *entry file* is scanned, not the
  rest of the uploaded tree. `system_packages` (apt) are charset-sanitized against shell
  injection but are **not an allowlist**. (An upload's `entry_file` must be a bare `*.py`
  name; path traversal there was fixed on 2026-10-13.)
- **Registration `/internal/*` has no access check.** `POST /internal/spawned` records a PID and
  publishes the card of an approved agent waiting for that launch (`supervisor.finalize_spawned`;
  an unapproved, superseded or repeated launch is stopped instead, and a backend without a host
  loop ignores the report), so anything that reaches the route can report a PID for an approved
  agent; `GET /internal/stop-queue` drains pending stops. A report the service does not take has
  its PID queued for the host loop to stop, and only PIDs above 1 are accepted (the loop signals
  only PIDs it launched). Since 2026-10-13 the chat app's `/register/{path}` pass-through refuses
  `internal/` paths (404, regression-tested), so signed-in users can no longer reach them through
  the front door, but anything that reaches `:8091` directly still can. A loopback check in
  registration would not help (the proxy and the ENGR spawn loop both connect from localhost); a
  shared secret sent by `deployment/spawn_agent_loop.py` would. The routes exist on GCP too,
  where registration is IAM-private.
- **`coding_agent` runs mini-swe-agent in YOLO mode** (bash without confirmation),
  contained only by a per-request scratch workspace. Run it in a container/VM; in
  multi-tenant use it needs a strong per-request sandbox (gVisor/Kata/microVM).
- **`geoSpatial_agent/attack_bundle/` is an offensive research tool** (DoxBench-style
  image geolocation / re-identification) doubling as the production location engine.
  Be aware of its doxxing power.
- **Per-user secrets are decrypted and injected into unauthenticated specialists.** Keys
  are AES-256-GCM encrypted at rest (`chat_app/store/crypto.py`, `CREDENTIALS_ENCRYPTION_KEY`)
  — good — but at request time the chat app **decrypts** them and the planner injects them into
  the A2A task context in plaintext (`chat_app/server.py::_inject_credentials` → task `metadata`).
  Injection is **scoped** to the creds the target agent *declared* and the user *granted*
  (`chat_app/planner/runtime.py::_scope_credentials`); an agent declaring none gets none. Since 2026-10-13 the
  only exception is an opt-in: agents named in `PLANNER_UNSCOPED_CREDENTIAL_AGENTS` (the email
  and geo agents in `deployment/.env.example`) still get the whole bundle. That list matches on
  `agent_id`, the slug of the card name, so an approved custom agent with the same name would
  match too.
  Specialist ports remain unauthenticated, so a rogue local process could still receive whatever
  was injected for a given call. Skeleton-engine agents strip the `credentials`, `authorization`,
  `secret(s)` and `token` keys from request, message and part metadata and DataPart data, and the
  user's identity (`user_id`, `auth_user_id`, `mail`, `eppn`, `remote_user`/`remote-user`) from
  metadata, before building the LLM prompt (`agent_skeleton/executor.py::_model_visible_metadata`;
  `agent_skeleton/tests/test_executor_metadata.py`). Other metadata (channel, skill hints, task
  input, `user_goal`) still reaches the model provider, and agent images built from a commit
  before this fix keep the old executor until rebuilt. (Mitigations present: `agent_skeleton/system_tools/call_endpoint.py` reads its
  token from an env var at call time — never storing it — and redacts secret-shaped strings from
  error bodies.)
- **Email agent Google OAuth client files** are no longer baked into the image: the current
  `email_agent/Dockerfile` (agent-directory) expects `deployment/google_credentials/` mounted at
  runtime. Move them to a secret manager / Workload Identity.
- **User bug reports carry personal data.** `POST /issues` files to `GITHUB_ISSUES_REPO`, and
  the issue body includes the reporter's email, recent prompts and trace. Since 2026-10-13 the
  repo has no default (unset → 502, no GitHub call); it must be a private repo.
- **Policy write-gating can be skipped.** The planner defaults to `policy_mode=permissive`;
  even in `strict`, `chat_app/planner/runtime.py::_evaluate_policy` only gates a write skill when a precise
  skill hint is present — no hint ⇒ no confirmation. Set `strict` and close the no-hint
  gap for any shared deployment.
- **`.env`-based secrets, no secret manager.** API keys / OAuth web-client secrets /
  `CREDENTIALS_ENCRYPTION_KEY` live in `deployment/.env`. `.dockerignore` keeps `.env`,
  key and credential-file patterns out of this repo's image build contexts. Fine on a trusted
  node; externalize before scaling.
- **On the subprocess backend, any signed-in user can start an agent without approval.** Only
  uploaded-code agents wait for admin approval (`registration_service/server.py`, `carries_upload`).
  An `llm` agent with `http` tools, or a `proxy` agent, starts at once as a subprocess of the
  registration service, with its whole environment (`backend.py`, `env = {**os.environ}`) and its
  network namespace. Such an agent can send any variable named in `auth_env` (the LLM key
  included) to any URL, and can call the chat app and registration on `localhost` with identity
  headers of its choosing. This applies to the dev stack and Docker Compose (no `SPAWN_BACKEND`);
  the cluster and Cloud Run backends refuse non-upload agents (`supervisor.py`). Until approval
  covers these agents, give accounts only to people you would trust as admins.

---

## 3. Reliability hazards (not security, but they bite)

- **Stored-but-not-routed.** An ADS record can be `dirctl push`ed (stored) yet never
  land in the routing index (discoverable). The good publish paths
  (`agentnet/directory/publisher.py`, `agentnet/directory/backends.py`) **poll routing**
  to catch this; several per-agent inline publishers are fire-and-forget and can leave an
  agent invisible with no error. Repair/watch tooling lives in
  `agentnet/scripts/` (`scripts/ads -diag` / `-repair` / `-watch`).
- **Copies of the card/A2A/directory code.**
  - **This repo:** the directory client, card-to-record converter, publisher and A2A client are
    one package, `agentnet/` (the chat app's copy, `agent_directory_service/` and `ads_utils/`
    were merged into it). Still separate here: the Agent Cards themselves are built in several
    places (the registration service builds the card it publishes in `supervisor.py` and the
    `card.json` it commits with a built agent in `builder.py`; its handlers and
    `agent_skeleton/container_runner.py` build the cards agents serve;
    `registration_service/tests/test_streaming_agents.py` checks that served and published
    cards agree), and the wrapper generator keeps its own `oasf:` tag parser
    (`agent_skeleton/generator/oasf.py`).
  - **The `agent-directory` repo** has its own copies: the `_vendor_v2/` trees in
    coding/intelligence, inline copies in email/disaster/geospatial, scout's
    `scout/network/`, its own `ads_utils/` (which differs from `agentnet/`), and the copy of
    `agent_skeleton` in each `registered_agents/` folder. Some of the inline copies **drop the
    write-risk detection** that `agentnet`'s converter computes.

  A policy or security fix in one copy does not reach the others. Check all of them when fixing
  anything security-relevant.
- **Ephemeral vLLM + gateway↔node wiring.** The GPU node hostname changes each run and the
  gateway upstream must be repointed/tunneled — a manual, break-prone step
  (`DOWNSTREAM_CONSIDERATIONS.md` #3).

---

## 4. Future hardening checklist (before broader exposure)

Tracked in detail in [`docs/deployment/DOWNSTREAM_CONSIDERATIONS.md`](docs/deployment/DOWNSTREAM_CONSIDERATIONS.md) #2–#3; summary:

1. **Real A2A authn/authz** — populate `securitySchemes` and enforce at the planner *and*
   each specialist; stop binding specialists on an open `0.0.0.0` with no auth.
2. **Strong per-request sandboxing** for `coding_agent` (and any uploaded code) — microVM /
   gVisor / per-request pod, not a scratch dir.
3. **Externalize secrets** — secret manager / mounted secrets / Workload Identity; no baked
   Google creds, no env-only shared keys; rotate `CREDENTIALS_ENCRYPTION_KEY`.
4. **Per-conversation isolation — DONE.** The shared server context was dropped: contexts are
   per-user-keyed with ownership checks, identity fails closed, and history + pending state moved
   to Postgres (see [`docs/AUTH.md`](docs/AUTH.md) §5). Since 2026-10-13 a login identity always
   wins over a request-body `user_id`, and only the conversation owner can answer a pending
   action: `POST /confirmations` and a chat "yes" or "no" both return 403 to anyone else (fixed
   2026-10-15). An answer claims the pending action before acting, so two answers arriving
   together (a double click, a retry) run it once (fixed 2026-10-15), also when they reach
   different chat app processes, and the confirm button answers only the prompt it was shown on
   (fixed 2026-10-16). Still open here: set a non-permissive `policy_mode` and close the no-hint
   write-gate gap.
5. **Stable gateway + service DNS/auth** in front of the ephemeral node; unify the two
   directories (dirctl `:8888` vs scout's gRPC `:8889`).
6. **Converge the duplicated converter/A2A stack**: this repo now has one (`agentnet/`); move
   the `agent-directory` repo's copies onto it so security/policy logic has one source of truth.
