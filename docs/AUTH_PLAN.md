# Authentication & Credential Storage Plan

## Overview

Add user authentication to the planner (`agenticweb.engr.wustl.edu`, `:8090`) and
per-user encrypted credential storage. The planner is the single entry point — it
already proxies the registration service under `/register/*` — so auth middleware
at the planner layer gates everything automatically.

This is designed in two phases:
- **Phase 1 (this plan):** email/password login restricted to `@wustl.edu`, per-user
  credential storage, Google OAuth connect flow.
- **Phase 2 (future):** swap login to WashU SSO (OIDC). Session management and
  credential storage are untouched; only the login flow changes.

---

## Architecture

### Where things live

| Concern | Service | Notes |
|---|---|---|
| Auth middleware | `planner_agent` `:8090` | Gates all routes including proxied `/register/*` |
| Auth endpoints (`/auth/*`) | `planner_agent` `:8090` | Login, signup, logout, Google OAuth |
| User/session/credential DB | Shared Postgres | Same instance as registration service (`/engrfs/project/di2/postgres_data`) |
| Planner DB connection | New pool in `planner_agent` | Connects over TCP to registration container's Postgres |
| Credential encryption | `planner_agent/main_agent/crypto.py` | AES-256-GCM, key from env var (`cryptography` already in requirements.txt) |

### Request flow (authenticated)

```
Browser → :8090 (planner)
  → auth middleware checks session cookie
      → valid: continue to route
      → invalid/missing: redirect to /auth/login
```

### Google OAuth flow

```
User clicks "Connect Google Account"
  → GET /auth/google  (planner redirects to Google consent page)
  → User approves on Google
  → GET /auth/google/callback?code=...  (Google redirects back)
  → Planner exchanges code for access_token + refresh_token
  → Tokens encrypted and stored in user_credentials table
  → User redirected back to app
```

### How agents get user credentials (Planner Injection)

The planner fetches the requesting user's credentials from the DB at the start of
each `POST /messages` request and injects them into the A2A task payload sent to
specialist agents. Agents receive credentials as part of task context and use them
for that request. Agents remain completely auth-unaware.

```
User sends message → planner looks up user from session cookie
  → fetches + decrypts user's credentials from DB
  → injects into A2A task context (e.g. { openai_api_key, google_tokens })
  → specialist agent uses injected credentials for its LLM/API calls
```

---

## Database Schema

Three new tables added to the existing Postgres DB via `_SCHEMA_SQL` in
`planner_agent/main_agent/db.py` (new file, mirrors the pattern in
`registration_service/db.py`).

```sql
CREATE TABLE IF NOT EXISTS users (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email          TEXT NOT NULL UNIQUE,   -- enforced @wustl.edu at app layer
    password_hash  TEXT,                   -- nullable: unused after SSO migration
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,           -- random 32-byte hex string
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,       -- 24h from creation
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_credentials (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,            -- 'openai', 'google_oauth', 'anthropic', etc.
    encrypted    TEXT NOT NULL,            -- AES-256-GCM ciphertext (base64-encoded JSON)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, provider)
);
```

The `encrypted` column stores a base64-encoded AES-256-GCM ciphertext. Before
encryption the value is a JSON object — shape varies by provider:

```json
// provider = 'openai'
{ "api_key": "sk-..." }

// provider = 'google_oauth'
{ "access_token": "...", "refresh_token": "...", "expires_at": "2026-..." }
```

---

## New Files

| File | Purpose |
|---|---|
| `planner_agent/main_agent/db.py` | Connection pool + schema for users/sessions/credentials |
| `planner_agent/main_agent/crypto.py` | AES-256-GCM encrypt/decrypt using `CREDENTIALS_ENCRYPTION_KEY` env var |
| `planner_agent/main_agent/auth.py` | FastAPI dependency `get_current_user`, session helpers |
| `planner_agent/main_agent/auth_router.py` | `/auth/*` endpoints (signup, login, logout, google flow) |
| `planner_agent/main_agent/credentials_router.py` | `/me/credentials/*` endpoints |
| `planner_agent/main_agent/web/login.html` | Login/signup page |

---

## Endpoints

### Auth (`/auth/*`)

| Method | Path | Description |
|---|---|---|
| `GET` | `/auth/login` | Serve login/signup page |
| `POST` | `/auth/signup` | Create account; validates `@wustl.edu`; sets session cookie |
| `POST` | `/auth/login` | Verify password; sets session cookie |
| `POST` | `/auth/logout` | Delete session; clear cookie |
| `GET` | `/auth/google` | Redirect to Google consent page |
| `GET` | `/auth/google/callback` | Exchange code for tokens; store in `user_credentials` |

### Credentials (`/me/credentials/*`)

| Method | Path | Description |
|---|---|---|
| `GET` | `/me/credentials` | List which providers the user has connected (no values) |
| `PUT` | `/me/credentials/{provider}` | Store or update an API key |
| `DELETE` | `/me/credentials/{provider}` | Remove a credential |

Values are never returned to the browser — only the planner backend decrypts and
uses them.

---

## Auth Middleware

A FastAPI middleware on the planner checks every request for a valid session cookie.
Exempt paths: `/auth/*`, `/health`, `/static/*`.

The `get_current_user` dependency is the only place session validation logic lives.
For SSO migration, only this function and the `/auth/*` router change.

```python
# Every protected route declares this — unchanged after SSO migration
@app.post("/messages")
async def messages(req: ..., user = Depends(get_current_user)):
    ...
```

---

## Encryption

`crypto.py` wraps Python's `cryptography` library (AES-256-GCM):

- Master key loaded from `CREDENTIALS_ENCRYPTION_KEY` env var (32 random bytes, base64-encoded)
- Each encrypt call generates a fresh random nonce; ciphertext stored as `base64(nonce + tag + ciphertext)`
- Decrypt reverses this; throws if key or ciphertext is wrong

Key generation (one-time, run locally):
```bash
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

Store the output in `.env.local` and as a cluster deployment env var. If this key
is lost, all stored credentials are unrecoverable — users would need to re-enter them.

---

## Environment Variables

### New variables (add to `deployment/.env`)

| Variable | Purpose |
|---|---|
| `CREDENTIALS_ENCRYPTION_KEY` | 32-byte base64 key for AES-256-GCM |
| `GOOGLE_CLIENT_ID` | OAuth 2.0 web client ID (from Google Cloud Console) |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 web client secret |
| `GOOGLE_REDIRECT_URI` | `https://agenticweb.engr.wustl.edu/auth/google/callback` |
| `REGISTRATION_DB_DSN` | Postgres DSN (shared with registration service) |

---

## Credential Migration: Personal → Per-User

### The problem

Currently `deployment/.env` contains personal credentials that are shared across
all users of the deployment. Every user implicitly uses the same accounts and
accrues charges against the same bills. This must be resolved before the system
is opened to external users.

### Classification of existing env vars

**Infrastructure secrets — stay in `.env`** (deployment-level, not user-specific):

| Variable | Reason |
|---|---|
| `OPENAI_BASE_URL` / `OPENAI_API_BASE` | Points to the vLLM server, not a personal account |
| `OPENAI_API_KEY=dummy` | Placeholder for vLLM (no real account) |
| `ADS_SERVER_ADDR` and related | Internal service addresses |
| `REGISTRATION_DB_DSN` | Infrastructure database |
| `CREDENTIALS_ENCRYPTION_KEY` | Server-side master key |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | App identity, not a user credential |
| All `*_HOST`, `*_PORT`, `*_URL` agent addresses | Internal networking |

**User credentials — move to `user_credentials` table, remove from `.env`:**

| Variable | Provider key | Notes |
|---|---|---|
| `BRAVE_API_KEY` | `brave` | Scout agent web search |
| `GOOGLE_API_KEY` | `google_api` | Geospatial agent |
| `GOOGLE_MAPS_API_KEY` | `google_maps` | Geospatial agent |
| `EMAIL_AGENT_EMAIL` | (metadata, not a secret) | Stored as user profile field instead |
| `GOOGLE_CALENDAR_TOKEN` / `GOOGLE_CALENDAR_CREDENTIALS` | `google_oauth` | Replaced entirely by per-user Google OAuth flow |

### `deployment/google_credentials/` removal

`deployment/google_credentials/credentials.json` and `token.json` are personal
Google OAuth tokens for a single account, currently baked into the deployment and
used by the email agent for all users. Once per-user Google OAuth is live:

1. The email agent reads Google tokens from the A2A task context (injected by planner)
2. `deployment/google_credentials/` is removed from the repo and deployment
3. The existing Desktop OAuth client (`agentic-web-dev`) used to generate these
   tokens becomes unused and can be deleted from Google Cloud Console

### Agent changes required

Each agent currently reads credentials from env vars. Once credential injection is
in place, agents must be updated to read from the A2A task context instead, and
must return a clear error (not silently fall back) if a required credential is
missing from the context:

| Agent | Credential to read from context (not env) |
|---|---|
| `email_agent` | `google_oauth` tokens |
| `scout_agent` | `brave` API key |
| `geoSpatial_agent` | `google_api`, `google_maps` API keys |
| `coding_agent` | No user credentials (uses vLLM) |
| `intelligence_agent` | No user credentials (uses vLLM) |
| `disaster_response_agent` | No user credentials (uses vLLM) |

Agents that use vLLM (coding, intelligence, disaster) continue to read
`OPENAI_BASE_URL` from env — that remains infrastructure config.

### Migration order

1. Implement auth + credential storage (this plan)
2. Update planner to inject credentials into A2A task context
3. Update each agent to read from context (one at a time, starting with email)
4. Remove migrated vars from `.env` and `.env.example`
5. Remove `deployment/google_credentials/`

---

## SSO Migration Path (Phase 2)

When WashU SSO (OIDC) is ready:

1. Add `GET /auth/sso` and `GET /auth/sso/callback` to `auth_router.py`
2. On callback: validate OIDC token, upsert user by email, issue local session (same table)
3. Remove `POST /auth/signup` and `POST /auth/login`
4. Make `users.password_hash` nullable (it already is)
5. `get_current_user`, all protected routes, credential storage — **unchanged**

The WashU restriction becomes implicit (SSO only issues tokens for WashU accounts)
so the `@wustl.edu` email check can be dropped.

---

## Postgres TCP Access

The registration container runs Postgres internally and currently only listens on
a Unix socket (`/tmp/postgresql`). The planner runs in a separate container and
cannot reach that socket. Since all containers share the same network namespace on
the Slurm node, the planner can connect over TCP via `localhost:5432` — but
Postgres must be configured to accept TCP connections first.

**Required change in `deployment/entrypoints/entrypoint-registration.sh`:**

During `initdb` (first run only), patch `postgresql.conf` to enable TCP alongside
the existing Unix socket:

```bash
# Add after the existing dynamic_shared_memory_type sed patch:
sed "s|#listen_addresses = 'localhost'|listen_addresses = 'localhost'|" \
    "$PGDATA/postgresql.conf" > "$PGDATA/postgresql.conf.tmp" \
    && mv "$PGDATA/postgresql.conf.tmp" "$PGDATA/postgresql.conf"

# Append pg_hba.conf entry to allow local TCP connections:
echo "host  all  all  127.0.0.1/32  trust" >> "$PGDATA/pg_hba.conf"
```

> **Note:** `trust` auth is safe here because the port is not exposed outside the
> container/node. No password is needed for localhost TCP.

**Planner DSN** (add to `deployment/.env`):
```
REGISTRATION_DB_DSN=postgresql://postgres@localhost:5432/registration
```

**Important:** if the Postgres data directory already exists on the cluster
(`/engrfs/project/di2/postgres_data` is non-empty), the `initdb` block is skipped
and these patches won't run. In that case, manually apply them once:

```bash
# SSH into the cluster, then:
echo "listen_addresses = 'localhost'" >> /engrfs/project/di2/postgres_data/postgresql.conf
echo "host  all  all  127.0.0.1/32  trust" >> /engrfs/project/di2/postgres_data/pg_hba.conf
```

Then restart the job — Postgres will pick up the new config on next start.

---

## What Is Not In Scope

- Rate limiting on auth endpoints (future hardening)
- Email verification on signup (WashU SSO makes this short-lived)
- Admin UI for managing users
- Token refresh for Google OAuth (access tokens expire ~1h; refresh logic needed in
  the email agent when it uses the stored tokens)

---

## Google OAuth App

- **Client type:** Web application
- **Name:** `agentic-web`
- **Authorized redirect URI:** `https://agenticweb.engr.wustl.edu/auth/google/callback`
- **Authorized JS origin:** `https://agenticweb.engr.wustl.edu`
- Client ID and secret are set as env vars; never committed to git
- The existing Desktop client (`agentic-web-dev`) is separate and used by the
  email agent's service account flow — leave it as-is
