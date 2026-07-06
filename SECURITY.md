# Security posture & hazards

Status of the AgenticNetwork demo as it runs **today** (single trusted Slurm node,
behind a WashU Shibboleth gateway). This is a **research/demo** posture — several
things that are fine on a trusted single node become hard requirements before any
broader exposure. Companion docs: repo-root [`CLAUDE.md`](CLAUDE.md) §8–§9 (auth +
caveats) and [`deployment/DOWNSTREAM_CONSIDERATIONS.md`](deployment/DOWNSTREAM_CONSIDERATIONS.md)
#2–#3 (the at-scale hardening plan). Update this file when the posture changes.

---

## 1. What is protected, and what isn't

| Surface | Auth today | Notes |
|---|---|---|
| Public URL `agenticweb.engr.wustl.edu` | **WashU Shibboleth SSO** (Apache `mod_shib`) | The one authenticated front door. TLS at nginx. |
| Planner `:8090` | **None of its own** — trusts Apache-injected `eppn`/`mail`/`remote-user` headers (`main_agent/auth.py`), fail-closed (401) when a DB is configured | Safe **only** behind the Shib proxy. Reached raw (or via an SSH tunnel) with a spoofed identity header, auth is bypassed. |
| Planner admin (`/admin/*`) | **SSO identity ∈ `ADMIN_EMAILS`** (`require_admin`); fail-closed (empty list → no admins) | The shared `X-Admin-Token`/`MAIN_AGENT_ADMIN_TOKEN` + `POST /admin/login` are retired. See [`docs/AUTH.md`](docs/AUTH.md) §2. |
| Specialist ports (9103, 9104, 19105–19108) | **None** — empty `securitySchemes`, bind `0.0.0.0` | Any process on the node's localhost can drive Gmail send, calendar/file writes, code execution, geolocation **directly**, skipping the planner's policy. |
| Registration service `:8091` | Admin endpoints fail-closed on `ADMIN_EMAILS` (dev-open under `FAKE_DB=1`); `/internal/*` localhost-only | Approval is the trust boundary for uploaded code (see §2). |
| ADS `:8888` / vLLM `:8100` | **None** | Trusted-LAN assumption. |

**Takeaway:** the security model is *"one authenticated gateway in front of a fully
trusted node."* Everything inside the node trusts everything else.

---

## 2. Active hazards (true today)

- **Uploaded custom code is not sandboxed by the validator.** `registration_service/validator.py`
  is **pure-AST and never executes** code; `subprocess`/`eval`/`os.system` only produce
  *advisory warnings*, not blocks. The real trust boundary is **admin approval +
  container isolation** — not the validator. Only the *entry file* is scanned, not the
  rest of the uploaded tree. `system_packages` (apt) are charset-sanitized against shell
  injection but are **not an allowlist**.
- **`coding_agent` runs mini-swe-agent in YOLO mode** (bash without confirmation),
  contained only by a per-request scratch workspace. Run it in a container/VM; in
  multi-tenant use it needs a strong per-request sandbox (gVisor/Kata/microVM).
- **`geoSpatial_agent/attack_bundle/` is an offensive research tool** (DoxBench-style
  image geolocation / re-identification) doubling as the production location engine.
  Be aware of its doxxing power.
- **Per-user secrets are decrypted and injected into unauthenticated specialists.** Keys
  are AES-256-GCM encrypted at rest (`main_agent/crypto.py`, `CREDENTIALS_ENCRYPTION_KEY`)
  — good — but at request time the planner **decrypts** them and injects them into the
  A2A task context in plaintext (`server.py::_inject_credentials` → task `metadata`).
  Injection is now **scoped** to the creds a user *granted* the target agent (not the whole
  bundle — `runtime.py::_scope_credentials`), but specialist ports remain unauthenticated, so
  a rogue local process could still receive whatever was injected for a given call. (Mitigations
  present: `agent_skeleton/system_tools/call_endpoint.py` reads its token from an env var at
  call time — never storing it — and redacts secret-shaped strings from error bodies.)
- **Email agent's Google OAuth creds are baked into the image** (`Dockerfile.email`
  `COPY deployment/google_credentials/`). Move to a mounted secret / Workload Identity
  (TODO already noted in that Dockerfile).
- **Policy write-gating can be skipped.** The planner defaults to `policy_mode=permissive`;
  even in `strict`, `runtime.py::_evaluate_policy` only gates a write skill when a precise
  skill hint is present — no hint ⇒ no confirmation. Set `strict` and close the no-hint
  gap for any shared deployment.
- **`.env`-based secrets, no secret manager.** API keys / OAuth web-client secrets /
  `CREDENTIALS_ENCRYPTION_KEY` live in `deployment/.env` (and some are baked). Fine on a
  trusted node; externalize before scaling.

---

## 3. Reliability hazards (not security, but they bite)

- **Stored-but-not-routed.** An ADS record can be `dirctl push`ed (stored) yet never
  land in the routing index (discoverable). The good publish paths
  (`ads_utils/publisher.py`, planner backend, `agent_skeleton/serve.py`) **poll routing**
  to catch this; several per-agent inline publishers are fire-and-forget and can leave an
  agent invisible with no error. Repair/watch tooling lives in
  `agent_directory_service/scripts/` (`run_ads.sh -diag` / `-repair` / `-watch`).
- **~5–6 divergent copies of the card/A2A/directory stack** (canonical
  `agent_directory_service` + `ads_utils`, the `_vendor_v2/` copies in coding/intelligence,
  the planner fork, and inline copies in email/disaster/geospatial). A policy/security fix
  in one may not propagate — e.g. some inline copies **drop the write-risk detection** the
  canonical converter computes. Check all copies when fixing anything security-relevant.
- **Ephemeral vLLM + gateway↔node wiring.** The GPU node hostname changes each run and the
  gateway upstream must be repointed/tunneled — a manual, break-prone step
  (`DOWNSTREAM_CONSIDERATIONS.md` #3).

---

## 4. Future hardening checklist (before broader exposure)

Tracked in detail in [`deployment/DOWNSTREAM_CONSIDERATIONS.md`](deployment/DOWNSTREAM_CONSIDERATIONS.md) #2–#3; summary:

1. **Real A2A authn/authz** — populate `securitySchemes` and enforce at the planner *and*
   each specialist; stop binding specialists on an open `0.0.0.0` with no auth.
2. **Strong per-request sandboxing** for `coding_agent` (and any uploaded code) — microVM /
   gVisor / per-request pod, not a scratch dir.
3. **Externalize secrets** — secret manager / mounted secrets / Workload Identity; no baked
   Google creds, no env-only shared keys; rotate `CREDENTIALS_ENCRYPTION_KEY`.
4. **Per-conversation isolation — DONE.** The shared server context was dropped: contexts are
   per-user-keyed with ownership checks, identity fails closed, and history + pending state moved
   to Postgres (see [`docs/AUTH.md`](docs/AUTH.md) §5). Still open here: set a non-permissive
   `policy_mode` and close the no-hint write-gate gap.
5. **Stable gateway + service DNS/auth** in front of the ephemeral node; unify the two
   directories (dirctl `:8888` vs scout's gRPC `:8889`).
6. **Converge the duplicated converter/A2A stack** so security/policy logic has one source
   of truth.
