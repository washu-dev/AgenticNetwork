# Architectural Decisions

A log of the durable decisions that shaped AgenticNetwork — the *why* behind choices that
aren't obvious from the code. Newest first. Each entry: the decision, the context that
forced it, the rationale, and where it lives in code. When a decision is later reversed or
superseded, mark it and add a new entry rather than editing history.

> This replaces the rationale that used to be scattered across the (now local-only)
> planning docs in `docs/dev/`. If you're about to re-litigate one of these, read the
> entry first.

---

## D11 — Conversation history and pending confirmations live in Postgres, not process-local state
**Decision.** Per-conversation history (`conversation_messages`) and pending write/consent
confirmations (`pending_confirmations`) are stored in Postgres, write-through cached in the
in-process store and rehydrated on a cache miss. **Rationale.** The planner was process-local
(a SQLite file for history, an in-memory dict for pending state), so it couldn't run as more
than one process and lost continuity across restarts. Moving the durable state to the shared
DB makes the planner horizontally scalable and lets a conversation — or a mid-flight
confirmation — resume on any process or after a restart. Persistence is **best-effort**: a DB
hiccup degrades to the process-local cache rather than failing the turn. The only remaining
process-local state is the write-only `invocations` audit (never read back). **Code.**
`planner_agent/main_agent/conversation_store.py`, `pending_store.py`, `db.py` (schema),
`runtime.py` (`_open_context` rehydrate, `_save/_get/_clear_pending`).

## D10 — Per-user working context is keyed by `context_id` and bound to its owner
**Decision.** Each conversation has its own context, keyed by a server-minted `context_id`
and bound to the creating user; a request presenting another user's `context_id` is rejected
(403). **Context.** The planner originally kept a *single global* server context that every
user shared — a data-mixing and info-leak bug that bit as soon as two users were active.
**Rationale.** Multi-user correctness requires per-user isolation; ownership must be asserted,
not assumed. Unknown/stale ids start a fresh owned context (so a restart doesn't 404 a user).
**Code.** `runtime.py::_open_context` + `ContextOwnershipError`; `state/store.py` (the global
slot was removed).

## D9 — Identity fails closed once a database is configured
**Decision.** A request with no Shibboleth identity is a **401** when `REGISTRATION_DB_DSN` is
set, unless `PLANNER_ALLOW_ANON=1`. Without a DB (laptop/tests), it falls back to `demo-user`.
**Rationale.** Credentials, grants, context ownership, and admin all key on the identity;
silently collapsing anonymous callers to a shared `demo-user` would misattribute and leak
across users. Fail-closed only where a real deployment (a DB) exists keeps local dev friction-
free. **Code.** `server.py::_resolve_shib_user_id` / `_anon_identity_allowed`.

## D8 — Admin is the SSO identity in an `ADMIN_EMAILS` allowlist (no shared token)
**Decision.** Admin = an authenticated user whose email ∈ `ADMIN_EMAILS` (comma-separated),
enforced by `require_admin` (401 if no identity, 403 if not allowlisted). The old shared
`X-Admin-Token` / `MAIN_AGENT_ADMIN_TOKEN` and `POST /admin/login` are retired. **Context.**
The shared token was **open when unset** — a silent hole. **Rationale.** Deriving admin from
the same SSO identity removes a second secret to manage and can't be left accidentally open;
it fails closed (empty allowlist → no admins). Enforced in both the planner and the
registration service (dev-open under `FAKE_DB=1`). **Code.** `auth.py::is_admin`/`admin_emails`,
`server.py::_require_admin` + the `/register/*` proxy gate, `registration_service/server.py::_require_admin`.

## D7 — Public landing page; the chat UI is hard-gated at `/app`
**Decision.** `/` serves a public landing page (no identity); the chat UI moves to `/app`,
gated at the planner (401 without identity when a DB is set) in addition to the Apache SP.
**Rationale.** The old setup auto-dropped every visitor straight into chat with no front door;
an explicit "Sign in with WashU" landing + a planner-side gate (defense-in-depth alongside the
SP) is the intended entry flow. Requires an Apache SP carve-out to make `/` anonymous. **Code.**
`server.py` (`/` vs `/app`), `web/landing.html`; SP carve-out in `docs/deployment/engr/README.md`.

## D6 — Two credential-sourcing models: operator env-var vs. per-user
**Decision.** Credentials come from one of two models, chosen by *who owns the resource*:
**operator-owned** (a shared service/infra key — Brave, Maps, vLLM) is read from an env var and
never stored by us; **user-owned** (a user's Gmail/GitHub/OpenAI account) is stored per-user
(encrypted), granted per-agent, and injected per-request into the task context. **Context.**
The endpoint-wrapper lane (Ronak) reads a token from an env var; the uploaded-agent lane (P4)
injects per-user credentials. These are the two skeleton executors (`SkeletonAgentExecutor` vs
`HandlerExecutor`) — they coexist. **Rationale.** A shared token on a user-owned resource breaks
attribution/isolation/billing; a per-user key on a shared resource is pointless friction.
**Open question:** the generator only emits the operator model today; wiring per-user into the
endpoint-wrapper lane is deferred. **Code.** `agent_skeleton/system_tools/call_endpoint.py`
(operator) vs `agent_skeleton/handler_executor.py` (per-user). See also [AUTH.md](AUTH.md) §4 and
[BUILDING_AGENTS.md](BUILDING_AGENTS.md).

## D5 — Per-user credentials: declared on the card, granted, scoped, injected out-of-band
**Decision.** A custom agent *declares* the credentials it needs; the user *grants* them
per-agent (first-use consent); the planner *scopes* injection to granted+declared creds for the
target agent and passes them in the A2A task metadata — never through the LLM. **Rationale.**
Least-privilege (each agent sees only what it was granted, not all a user's keys) and the LLM
never sees or solicits secrets. Declaration rides in `capabilities.extensions` (a native,
round-tripping card field) because ADS discovery discards the `annotations` map. **Code.**
`registration_service/card_credentials.py` (declare), `db.py::agent_credential_grants` (grant),
`runtime.py::_enforce_credentials`/`_scope_credentials` (scope+inject), skeleton
`handler_executor.py` (read).

## D4 — Credential namespace: catalog `type` is canonical (Option A)
**Decision.** The credential catalog `type` values (`openai_api_key`, `brave_api_key`, …, plus
`custom:<slug>`) are the single namespace; the planner store's `provider` column holds those
strings, with an idempotent migration rewriting legacy values (`openai`→`openai_api_key`, …).
**Context.** The catalog and the live store used different strings, so injection wouldn't match.
**Rationale.** One canonical namespace makes injection deterministic and the declared `env_var`
names what the agent reads it under. **Code.** `credential_catalog.py` (both services),
`credentials_router.py` (accepted set), `db.py` (legacy-value migration).

## D3 — Custom agents have two upload lanes
**Decision.** A custom agent is either **uploaded code** (a handler file/zip run by
`HandlerExecutor`, gated by admin approval + container isolation) or an **endpoint-wrapper**
(a spec-driven agent that proxies an external HTTP/A2A endpoint, generated from an
`answers.json`). **Rationale.** Wrapping an existing API shouldn't require writing/uploading
code; running arbitrary uploaded code needs the approval trust boundary. The two lanes use
distinct executors and distinct credential models (see D6). **Code.**
`agent_skeleton/handler_executor.py` + `registration_service/handlers/custom_handler.py`
(uploaded); `agent_skeleton/spec.py` + `executor.py` + `generator/` (wrapper).

## D2 — Uploaded code's trust boundary is admin approval + container isolation, not the validator
**Decision.** The upload validator (`registration_service/validator.py`) is pure-AST and never
executes code — `subprocess`/`eval`/`os.system` produce *advisory warnings*, not blocks. Custom
agents start un-approved and only spawn after an admin approves. **Rationale.** Static analysis
can't safely sandbox arbitrary Python; the real containment is human review + running each agent
in its own container. **Code.** `registration_service/validator.py`, the approve/reject flow,
`SECURITY.md` §2.

## D1 — ADS records are both *stored* and *routed*; the backend fails closed
**Decision.** A directory record must be both content-addressably stored **and** present in the
routing index to be discoverable; the directory backend raises `DirectoryUnavailable` rather
than silently falling back to storage search. **Rationale.** A "stored but not routed" record is
invisible to live discovery — a silent failure mode. Failing closed surfaces it; the good
publish paths poll routing to catch it. **Code.**
`agent_directory_service/services/directory/backends.py`, the `scripts/` repair suite.
