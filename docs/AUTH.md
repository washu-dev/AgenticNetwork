# Authentication, Credentials & Multi-User Isolation

The identity, admin, per-user-credential, and multi-user-safety model of the planner. This is
the deep version of [`CLAUDE.md`](../CLAUDE.md) §8; the *why* behind each choice is in
[DECISIONS.md](DECISIONS.md) (D5–D11).

Everything here is **planner-only** — specialist agents remain auth-unaware (see
[`SECURITY.md`](../SECURITY.md) for the resulting node-trust caveats).

## 1. Identity (Shibboleth)

The planner never handles passwords. A WashU Shibboleth SP (Apache `mod_shib`) in front of it
validates the SAML assertion and injects identity headers; the planner trusts them.

```
Browser → nginx (TLS) → Apache + mod_shib (Shibboleth SP) → reverse-proxy → planner :8090
                          └─ injects mail / eppn / remote-user headers
```

- `auth.py::get_shib_email` reads headers in priority `mail → eppn → remote-user` (lowercased).
- `auth.py::get_or_create_user` auto-provisions a `users` row on first login.
- `auth.py::get_current_user` is the FastAPI dependency (raises 401 if no identity).
- The gateway (nginx/Apache/Shibboleth) is **persistent infra outside this repo**; it proxies
  to the planner on an ephemeral Slurm node, so its upstream is repointed each run.
- Locally, the dev stack's identity proxy (`devstack/dev_identity_proxy.py`) plays the gateway:
  it drops client identity headers and injects `mail: dev@example.test` ([DEVSTACK.md](DEVSTACK.md)).

> **On GCP (front door not built yet).** The GCP planner has **no SSO gateway in front of it**
> today — it runs `PLANNER_ALLOW_ANON=1` and is reached IAM-privately via `gcloud run services
> proxy` (operators only). It *is* now on Cloud SQL, so the credential/history persistence and the
> fail-closed machinery below are live; they just aren't enforced while anon is on. The GCP front door
> is not built yet (GCP work has been on hold since end of summer 2026; cloud is a critical leg going
> forward, 2026-10-13); the decided approach is **Option A**
> — the same WashU Shibboleth gateway repointed at GCP, minting a Google ID token to reach the private
> planner (drops anon at cutover). See
> [`docs/deployment/gcp/README.md`](deployment/gcp/README.md) "How to access" + "Status & deferred work (handoff)" and
> [`docs/deployment/DOWNSTREAM_CONSIDERATIONS.md`](deployment/DOWNSTREAM_CONSIDERATIONS.md) #4.

### Fail-closed identity (D9)
`server.py::_resolve_shib_user_id` is the single identity resolver for `/messages`,
`/confirmations` and the data endpoints; a header identity always wins over a request-body
`user_id`. When no header is present:
- **DB configured** (`REGISTRATION_DB_DSN` set) → **401**, unless `PLANNER_ALLOW_ANON=1`.
- **No DB** (laptop/tests) → the body `user_id` on `/messages` and `/confirmations`, `demo-user`
  elsewhere.

This matters because credentials, grants, context ownership, and admin all key on identity;
silently collapsing anonymous callers to a shared `demo-user` would misattribute and leak.

## 2. Admin (SSO + `ADMIN_EMAILS` allowlist) — D8

Admin is **not** a separate password. It's derived from the same SSO identity:

- `auth.py::admin_emails()` parses `ADMIN_EMAILS` (comma-separated, lowercased). Empty ⇒ **no
  admins** (fail closed).
- `auth.py::is_admin(email)` = membership test.
- `server.py::_require_admin` = `_resolve_shib_user_id` (401 if no identity) → `is_admin` else
  **403**. It gates all `/admin/*` routes **and** the admin `/register/*` proxy subpaths
  (`approve`, `reject`, `handler`, `handler/download`).
- `registration_service/server.py::_require_admin` re-checks the forwarded identity as
  defense-in-depth (dev-open under `FAKE_DB=1`).
- `/me` returns `is_admin`; the UI shows the Admin entry / dashboard only to admins.

The old shared `X-Admin-Token` / `MAIN_AGENT_ADMIN_TOKEN` and `POST /admin/login` are **retired**
(they were open-when-unset — a silent hole). Set `ADMIN_EMAILS` once in the shared
`deployment/.env` (both services read it).

## 3. Serving split: public landing, gated app (D7)

- `GET /` → public landing (`web/landing.html`), no identity required.
- `GET /app` → the chat UI, **hard-gated** at the planner via `_resolve_shib_user_id` (401
  without identity when a DB is set) — defense-in-depth alongside the Apache SP.
- Logout (`auth_router.py`) returns through `/Shibboleth.sso/Logout?return=/` to the landing.
- Making `/` truly anonymous requires an Apache SP carve-out (public `/` + `/health`, protected
  everything else) — documented in [`docs/deployment/engr/README.md`](deployment/engr/README.md).

## 4. Per-user credentials

The mechanism for giving a user's own API keys to the agents that need them, without the LLM
ever seeing a secret. Gated on `REGISTRATION_DB_DSN` — inert without a DB.

### Storage
- `db.py::user_credentials(user_id, provider, encrypted, …)`, `UNIQUE(user_id, provider)`.
- `crypto.py` AES-256-GCM (`CREDENTIALS_ENCRYPTION_KEY`, 32-byte base64). API keys stored as
  `{"api_key": …}`, OAuth as the token bundle. Values are never returned by list endpoints.
- `credentials_router.py` (`/me/credentials/*`) manages keys; the OAuth callback writes
  provider `google_oauth`.

### Namespace (Option A — D4)
The credential catalog `type` values (`openai_api_key`, `brave_api_key`, …, plus `custom:<slug>`)
are the **canonical** namespace. The `user_credentials.provider` column stores those strings; an
idempotent migration in `db.py` rewrites legacy values (`openai`→`openai_api_key`, …).
`credential_catalog.py` is mirrored in both the registration service and the planner.

### The flow: declare → grant → scope → inject → read (D5)
1. **Declare.** A custom agent declares required credentials; the registration service writes
   them onto the published card's `capabilities.extensions` under
   `agenticnetwork.dev/ext/required-credentials` (`registration_service/card_credentials.py`).
   Extensions are used because ADS discovery discards the `annotations` map.
2. **Discover.** The planner reads them back into `DiscoveredAgent.required_credentials`
   (`agentnet/directory/card_oasf.py::_required_credentials_from_card`, the read path).
3. **Enforce + consent.** In `runtime.py::_enforce_credentials` (before dispatch):
   - required + not stored → **block** with "add it in Settings."
   - required + stored + not granted → **consent prompt** (`kind="credential_consent"`); on
     "yes", a grant is recorded and the call proceeds.
   - optional-missing → proceed. Agents that declare **no** credentials get none, unless their
     id is in `PLANNER_UNSCOPED_CREDENTIAL_AGENTS` (legacy opt-in for the email/geo agents,
     which get the whole bundle).
4. **Grant.** `db.py::agent_credential_grants(user_id, agent_id, cred_type)` — persisted,
   revocable via `/me/grants/*`.
5. **Scope + inject.** `runtime.py::_scope_credentials` selects only the creds that are
   **declared by the target agent AND granted by this user**, keyed by the declared `env_var`,
   and places them in the A2A task metadata (`MessageSendParams.metadata["credentials"]`) —
   **never** through the LLM. The system prompt forbids the model from soliciting secrets.
6. **Read.** The custom agent reads them from its request context — see the skeleton seam in
   [BUILDING_AGENTS.md](BUILDING_AGENTS.md).

Google OAuth token refresh **is** implemented (`server.py::_refresh_google_token`).

### Two credential-sourcing models (D6)
Which model applies depends on **who owns the resource**:
- **Operator-owned** (a shared service/infra key — Brave, Maps, vLLM): read from an env var at
  call time, never stored by us. This is the **endpoint-wrapper** lane
  (`agent_skeleton/system_tools/call_endpoint.py`).
- **User-owned** (a user's Gmail/GitHub/OpenAI account): the per-user flow above. This is the
  **uploaded-agent** lane (`agent_skeleton/handler_executor.py`).

They map to the two skeleton executors and coexist. Wiring per-user creds into the
endpoint-wrapper lane is a deferred open question. See [BUILDING_AGENTS.md](BUILDING_AGENTS.md).

## 5. Multi-user isolation

The planner was originally single-user; three fixes made it safe for concurrent users, and the
durable state now lives in Postgres so it scales past one process.

- **Per-user context keying (D10).** Each conversation has its own `context_id`-keyed context,
  bound to its owner; presenting another user's `context_id` → 403 (`runtime.py::_owned_context`,
  `ContextOwnershipError`), for any message, a "yes" or "no" to a pending action included, and for
  `POST /confirmations`. The old single global context was removed.
- **Ownership on data endpoints.** `GET /sessions` and `GET`/`DELETE /conversations/{id}` derive
  the principal from identity (not a query param) and enforce ownership (403/404).
- **Durable, shared state (D11).** Conversation history (`conversation_store.py` →
  `conversation_messages`) and pending confirmations (`pending_store.py` →
  `pending_confirmations`) are in Postgres, write-through cached and rehydrated on a miss — so a
  conversation or a mid-flight confirmation resumes on any process or after a restart. An answer
  to a pending action first claims its row (one `UPDATE` that marks it, only if it is unclaimed
  and still the prompt the answer read) and deletes it once done. So two answers arriving
  together, on one process or two, run it once, an answer arriving meanwhile on any process gets
  `answer_in_progress`, and the confirm button, which names its prompt (`prompt_id`), never
  answers a newer one. If the claim statement fails, the answer is refused
  (`pending_store_unavailable`) and nothing runs (details: [`PLANNER.md`](PLANNER.md),
  "Confirmation & consent resume").
  Persistence is best-effort (a DB hiccup degrades to the cache, never fails the turn), except
  an answer's claim, which fails closed. The only process-local state left is the write-only
  `invocations` audit.

See [PLANNER.md](PLANNER.md) for how these fit into the runtime.
