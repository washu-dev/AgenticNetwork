# Custom Agent Lane v2 — P4 Subplan: Per-User Credentials + Auth Restructuring

**Status:** IN PROGRESS. **P4a shipped** (2026-07-02, branch `feature/custom-agent-credentials-and-auth`):
credential catalog + `CredentialRequirement` model + card carrier (`capabilities.extensions`), registration
UI field, `/credential-catalog` endpoint, approver visibility; verified locally (91 tests) and on the cluster
(Level 1 — declaration round-trips + persists). Deviations from this plan, all deliberate: extension-level
`required=False` (enforcement driven by `params.credentials[].required`, not the A2A protocol flag);
extension omitted when no creds declared (decision A); added `GET /credential-catalog` as the UI's single
source of truth. Next: **P4b**.
**Parent:** [`CUSTOM_AGENT_LANE_V2_PLAN.md`](CUSTOM_AGENT_LANE_V2_PLAN.md) §8 (P4). P1/P2/P3 shipped.
**Branch:** a new branch off `main` after `feature/custom-agent-handler` is closed —
suggest `feature/custom-agent-credentials-and-auth`.
**Scope (two related tracks, one PR series):**
- **Track 1 — Credentials (P4a–P4d):** wire the per-user credential store for **newly-uploaded
  custom agents** — declare → grant → inject — reusing the mechanism Google OAuth already uses
  (`user_credentials` + `crypto.py` + `/me/credentials/*`).
- **Track 2 — Auth restructuring (P4e–P4g):** an explicit **SSO login landing page** (no more
  silent auto-sign-in), **admin via SSO + email allowlist** (retire the shared token), and
  **multi-user data separation** (fold in [`PLANNER_MULTIUSER_HARDENING_PLAN.md`](PLANNER_MULTIUSER_HARDENING_PLAN.md)
  — the planner currently shares one global conversation context across all users).

**Out of scope (unchanged):** migrating existing specialist agents (`BRAVE_API_KEY`,
`GOOGLE_API_KEY`, `GOOGLE_MAPS_API_KEY`) off env vars — a separate, intended future workstream.

This subplan supersedes parent §8 where they differ; the differences come from a fresh
read of the auth code (below), not the parent's stated assumptions.

**Locked decisions (this session, 2026-07-02):** namespace = **Option A** (catalog `type` is
canonical, extend the store; §4.5); admin = **SSO + email allowlist** (§10.2); both DB env vars
(`REGISTRATION_DB_DSN` + `CREDENTIALS_ENCRYPTION_KEY`) **are set in the cluster `deployment/.env`**
(confirmed by maintainer — §7).

---

## 0. Ground truth — what the auth/credential surface *actually* does today

Read directly from code on `feature/custom-agent-handler` (2026-07-02). **The parent plan's
§13 claim that "per-request credential injection into task context already exists → reduces P4
scope" is only half true.** Corrections are called out inline.

### 0.1 Identity (Shibboleth) — works
- [`auth.py`](../planner_agent/main_agent/auth.py): `get_shib_email()` reads headers in priority
  `mail → eppn → remote-user` (lowercased); `get_or_create_user()` auto-provisions a `users` row
  on first login; `get_current_user()` is the FastAPI dependency (raises 401 if no header).
- [`server.py:588`](../planner_agent/main_agent/server.py#L588) `_incoming_message_from_request`
  uses the Shib email as `user_id`, falling back to `"demo-user"` when no header is present.
- **Admin fallback:** `POST /admin/login` ([server.py:122](../planner_agent/main_agent/server.py#L122))
  compares against `MAIN_AGENT_ADMIN_TOKEN`; if that env var is empty, admin is fully open.

### 0.2 Everything credential-related is gated on `REGISTRATION_DB_DSN`
Both the auth/credentials routers **and** the DB lifecycle are only mounted when
`REGISTRATION_DB_DSN` is set ([server.py:136, 146](../planner_agent/main_agent/server.py#L136)).
With no DB: `/me/credentials/*` and `/auth/*` don't exist, and `_inject_credentials` returns
immediately. **P4 hard-requires Postgres reachable + `CREDENTIALS_ENCRYPTION_KEY` (32-byte
base64) set.** No DB → the whole feature silently no-ops. (Note the shared DB is the same one
the registration service uses — DSN env var name is `REGISTRATION_DB_DSN`.)

### 0.3 Storage — works, but a fixed provider whitelist
- Schema [`db.py:21`](../planner_agent/main_agent/db.py#L21): `user_credentials(id, user_id,
  provider TEXT, encrypted TEXT, created_at, updated_at)`, `UNIQUE(user_id, provider)`,
  `ON DELETE CASCADE` from `users`.
- API-key providers are whitelisted to **`{openai, anthropic, brave, google_api, google_maps}`**
  ([credentials_router.py:17](../planner_agent/main_agent/credentials_router.py#L17)); the OAuth
  callback additionally writes provider `google_oauth`.
- `crypto.py` AES-256-GCM (`encrypt`/`decrypt` a dict); API keys are stored as `{"api_key": ...}`,
  OAuth as the token bundle. Values are never returned by `list_credentials`.
- ⚠️ **Namespace mismatch to resolve (P4c):** the parent §8.1 catalog wants `type` keys like
  `openai_api_key`, `brave_api_key`; the live store uses `provider` values `openai`, `brave`,
  `google_api`, `google_maps`, `google_oauth`. The catalog `type` and the stored `provider` are
  **not** the same strings today. P4 must pick one namespace and translate, or injection won't
  match. See §4.5.

### 0.4 Injection — EXISTS but is (a) unscoped and (b) not readable by custom agents
This is the important correction.
- [`server.py:573`](../planner_agent/main_agent/server.py#L573) `_inject_credentials` fetches
  **all** of the user's decrypted creds, refreshes `google_oauth` if a refresh token is present
  (`_refresh_google_token` — **so OAuth refresh IS implemented**, contrary to CLAUDE.md §8's
  "not done" list), and sets `metadata["credentials"] = {provider: payload, ...}`.
- That metadata flows: `runtime.handle_message` → `_tool_call_agent(credentials=...)`
  ([runtime.py:430](../planner_agent/main_agent/runtime.py#L430)) → `_build_a2a_request` sets
  `request.metadata["credentials"]` ([runtime.py:899](../planner_agent/main_agent/runtime.py#L899))
  → `internal_task_request_to_a2a_request` spreads `**request.metadata` into
  **`MessageSendParams.metadata`** ([adapters.py:162](../planner_agent/main_agent/a2a/adapters.py#L162)).
  So credentials **do** reach the wire, at the JSON-RPC *params* metadata level (NOT the
  `Message.metadata` level — `_internal_message_to_sdk` copies only a hardcoded whitelist).
- **Gap 1 (scoping):** every called agent receives **ALL** of the user's credentials. There is no
  per-agent filtering. P4c must scope to *granted + declared* creds for the target agent.
- **Gap 2 (custom agents can't read them):** the skeleton's `HandlerExecutor.execute`
  ([handler_executor.py:60](../agent_skeleton/handler_executor.py#L60)) reads only
  `context.get_user_input()` + files and calls `handle_structured(user_input, files)` — a
  **2-arg** signature ([base.py:67](../agent_skeleton/base.py#L67)). It never touches request
  metadata. So a custom agent has **no way to receive credentials today**. P4d must add the seam.

### 0.5 Consent primitive — reusable but generic
`_tool_call_agent` already has a confirm/deny interrupt for `write` skills:
`PolicyDecisionStatus.requires_confirmation` → `store.save_pending_confirmation` + a terminal
payload asking "reply yes/no" ([runtime.py:561](../planner_agent/main_agent/runtime.py#L561)).
P4c reuses this *pattern*, but credential first-use consent is richer than yes/no ("you need to
add an OpenAI key in Settings, then grant it") — so it needs its own pending-state shape, not a
literal reuse.

### 0.6 Docs are stale — treat code as truth
- **`AUTH_PLAN.md` does not exist in the repo** although CLAUDE.md §8 and parent §8 both link it.
- CLAUDE.md §8 lists "Google OAuth token refresh" as not done — it **is** done (§0.4).
- Recommend: after P4, refresh CLAUDE.md §8 and either write `AUTH_PLAN.md` or drop the links.

### 0.7 Discovery read path — confirmed (matches parent §13)
`oasf_record_to_discovered_agent` ([card_oasf_converter.py:229](../planner_agent/main_agent/directory/card_oasf_converter.py#L229))
rebuilds the `AgentCard` from `modules[0].data.card_data` and keeps `raw_record`. The ADS
`annotations` map is discarded, there is no live card fetch, and `AgentCard` is `extra=ignore` —
so the declaration must ride in **`capabilities.extensions`** (a native, round-tripping field).
`_agent_full_card` ([runtime.py:1083](../planner_agent/main_agent/runtime.py#L1083)) hand-picks
fields, so adding one is safe and requires an explicit edit.

---

## 1. Sub-phases (independently shippable)

**Track 1 — Credentials**

| Sub-phase | Deliverable | Components | Depends on | Exit criterion |
|---|---|---|---|---|
| **P4a** ✅ | Credential **declaration** + catalog + card carrier | registration + skeleton contract | — | **DONE.** A custom agent declares creds; they land in the published card's `capabilities.extensions`; unit test round-trips extension→card. |
| **P4b** ✅ | Planner **discovery read** + LLM surface (read-only, no enforcement) | planner | P4a | **DONE.** `DiscoveredAgent.required_credentials` populated from the extension; visible in `_agent_full_card`; unit test with a stubbed record. |
| **P4c** ✅ | **Grant + consent + enforcement + scoped injection** | planner | P4b | **DONE (impl + unit tests; cluster gate pending).** Required-missing → blocked w/ consent; optional-missing → proceeds; injected creds scoped to *granted*; grants persist + revocable via API. |
| **P4d** ✅ | Skeleton **context seam** + **Settings UI** | skeleton + planner web | P4c | **DONE (impl + unit tests; cluster e2e pending).** A custom agent reads a user key from `context` and uses it; Settings tab manages keys + grants; `context` never logged. |

**Track 2 — Auth restructuring** (see §9–§11)

| Sub-phase | Deliverable | Components | Depends on | Exit criterion |
|---|---|---|---|---|
| **P4e** | **SSO login landing page** — public `/` with a "Sign in with WashU" button; chat UI moves behind a protected route | planner web + Apache/Shib config | — | Unauthenticated visit to `/` shows the landing page (no auto-sign-in); clicking sign-in runs SSO and lands on the chat UI; logout returns to the landing page. |
| **P4f** | **Admin via SSO + email allowlist** — retire the shared `X-Admin-Token`; admin role derived from Shibboleth identity ∈ `ADMIN_EMAILS` | planner | P4e | `/admin/*` + approver require an authenticated admin-allowlisted user; non-admins get 403; open-if-unset is gone. |
| **P4g** | **Multi-user data separation** — fold in [`PLANNER_MULTIUSER_HARDENING_PLAN.md`](PLANNER_MULTIUSER_HARDENING_PLAN.md) H1–H3 (per-user context keying, auth+ownership on data endpoints, fail-closed identity) | planner | P4e | Two interleaved users never see each other's conversation/goal; data endpoints enforce ownership; missing identity → 401 (not `demo-user`). |

Ship Track 1 in order; each is a reviewable PR. P4a+P4b are low-risk and non-enforcing (safe to
land early). P4c is the behavioral core. P4d closes the loop end-to-end. **Track 2 sequencing:**
land **P4g (F3 fail-closed identity) and P4e early** — they harden the identity foundation that
both tracks sit on; P4f depends on the landing/SSO flow (P4e). Track 2 can proceed in parallel
with Track 1 since credential isolation is already per-user-correct (§0.4 confirms).

---

## 2. P4a — Declaration, catalog, card carrier

### 2.1 Models ([registration_service/models.py](../registration_service/models.py))
```python
class CredentialRequirement(BaseModel):
    type: str                     # catalog key (§2.2) or "custom:<slug>"
    label: str                    # human text for consent/Settings, e.g. "OpenAI API key"
    required: bool = True         # D6: required→block if missing; optional→proceed
    env_var: str | None = None    # name the agent expects it under in context/env
```
Add to `CustomConfig`:
```python
credentials: list[CredentialRequirement] = Field(default_factory=list)   # D2/D3
```
(Non-secret config stays in `extra`; secrets are never in `extra` anymore.) Safe per parent
§13: config is schemaless JSONB, no `extra="forbid"`, no DB migration.

### 2.2 Catalog ([registration_service/credential_catalog.py](../registration_service/credential_catalog.py), NEW)
A maintained list of known types. Each: `type`, `label`, `kind` (`api_key`|`oauth`),
optional `docs_url`. Include a `custom:<slug>` escape (free-form label, `api_key` kind).
**Decision needed (§4.5):** align catalog `type` values with the planner store's `provider`
values so injection matches. Recommend the catalog is the single source of truth and the planner
imports/mirrors it.

### 2.3 Write the carrier — `capabilities.extensions`
The card the planner reads is built by `supervisor._build_agent_card`
([supervisor.py:669](../registration_service/supervisor.py#L669)). Append one extension built
from the declared creds:
```python
AgentExtension(
    uri="agenticnetwork.dev/ext/required-credentials",
    description="User credentials this agent consumes",
    required=any(c.required for c in creds),
    params={"credentials": [c.model_dump() for c in creds]},
)
```
into `AgentCapabilities.extensions`. **Also set `streaming=True` stays as-is** (P3). Mirror the
extension in the served-card builders (`container_runner.py`, `custom_handler.py`) for direct
callers, but the planner only reads the ADS-published one.

### 2.4 Registration UI + approver surface
- Add a "Required credentials" repeatable field to the custom form in
  [`registration_service/static/index.html`](../registration_service/static/index.html)
  (type dropdown from the catalog + `custom:` escape, label, required checkbox, optional env_var).
- Surface declared credentials in the inspect endpoint response (like P2's `system_packages`) so
  the approver sees what the agent will ask users for — [server.py get_handler_source](../registration_service/server.py#L324)
  and the approver panel in [`planner_agent/main_agent/web/index.html`](../planner_agent/main_agent/web/index.html).

### 2.5 P4a tests
- `CustomConfig` accepts `credentials`; round-trips through `AGENT_CONFIG_JSON`.
- `_build_agent_card` emits the extension with the right `uri` + `params.credentials`.
- Inspect endpoint returns declared credentials.

---

## 3. P4b — Planner discovery read + LLM surface (read-only)

### 3.1 Model ([planner_agent/main_agent/models.py](../planner_agent/main_agent/models.py))
Add to `DiscoveredAgent`:
```python
required_credentials: list[CredentialRequirement] = Field(default_factory=list)
```
(Mirror `CredentialRequirement` in the planner; or a shared tiny module. Optional/defaulted →
safe per parent §13.)

### 3.2 Populate in `oasf_record_to_discovered_agent`
([card_oasf_converter.py:229](../planner_agent/main_agent/directory/card_oasf_converter.py#L229)):
read the rebuilt `card.capabilities.extensions`, find the `agenticnetwork.dev/ext/required-credentials`
uri, parse `params["credentials"]` into `CredentialRequirement`s. Defensive: missing/absent →
empty list.

### 3.3 Surface to the LLM (D14)
Add `required_credentials` to `_agent_full_card` ([runtime.py:1083](../planner_agent/main_agent/runtime.py#L1083))
so the planner can explain to the user why a key is needed during first-use consent.

### 3.4 P4b tests
- Stub an OASF record whose embedded card carries the extension → `DiscoveredAgent.required_credentials`
  populated. Absent extension → empty. Malformed params → empty (no crash).
- `_agent_full_card` includes the field.

---

## 4. P4c — Grant, consent, enforcement, scoped injection (planner, the core)

### 4.1 Grants table
New table (in `db.py` `_SCHEMA_SQL`, `CREATE TABLE IF NOT EXISTS` — additive):
```sql
agent_credential_grants (
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,          -- DiscoveredAgent.agent_id
    cred_type  TEXT NOT NULL,          -- catalog type / provider (post-§4.5 alignment)
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, agent_id, cred_type)
)
```

### 4.2 Enforcement hook
In `_tool_call_agent`, **before `_build_a2a_request`** ([runtime.py:606](../planner_agent/main_agent/runtime.py#L606)),
after the agent is resolved and policy evaluated. Logic (D6):
1. Read `agent.required_credentials`. Empty → **strict no-op**, proceed (existing agents unaffected).
2. For each **required** cred: stored by the user? granted to this agent?
   - not stored → **block**: emit an `ask_user`-style terminal message telling the user to add the
     key in Settings (first-use consent). Do not dispatch.
   - stored but not granted → consent prompt to grant (remembered afterward, revocable in Settings).
3. Optional creds absent → proceed.
4. On grant, insert into `agent_credential_grants`.

Model the pending state on the existing `save_pending_confirmation` machinery
([runtime.py:561](../planner_agent/main_agent/runtime.py#L561)) but with a distinct
`kind: "credential_consent"` so the resume path knows to record a grant (not just set `confirmed`).

### 4.3 Scoped injection (closes Gap 1, §0.4)
`_inject_credentials` currently injects *all* creds globally at the request level. Two options —
**recommend (b)**:
- (a) keep global injection, filter at `_build_a2a_request` per target agent.
- (b) **preferred:** stop putting the full bundle on the request; instead, in `_build_a2a_request`
  (which knows the target `agent`), select only creds that are **declared by that agent AND granted
  by this user**, and place those under `metadata["credentials"]`, keyed by the declared `env_var`
  (or `type`) the agent expects. This makes injection least-privilege by construction.
- Existing specialist agents (email/geo/etc.) that rely on the *unscoped* google_oauth bundle: keep
  a compatibility path — if an agent declares **no** `required_credentials`, fall back to today's
  behavior so we don't break email. (Document this carve-out; it's the seam between the old and new
  models.)

### 4.4 Grants API (for the Settings UI in P4d)
Extend `/me/credentials/*` (or a new `/me/grants/*`):
`GET /me/grants` (list per-agent grants), `DELETE /me/grants/{agent_id}/{cred_type}` (revoke).
No secret values ever returned.

### 4.5 Namespace reconciliation — **LOCKED: Option A**
The catalog `type` (`openai_api_key`) ≠ the store `provider` (`openai`) today. **Decision (locked
2026-07-02): Option A — the catalog `type` is canonical.**
- Extend `_ALLOWED_PROVIDERS` ([credentials_router.py:17](../planner_agent/main_agent/credentials_router.py#L17))
  so the accepted set is the catalog `type` values (`openai_api_key`, `anthropic_api_key`,
  `brave_api_key`, `google_maps_api_key`, …) plus `custom:<slug>`.
- The `user_credentials.provider` column now stores catalog `type` strings.
- **Migration for existing rows:** map the legacy `provider` values to canonical types —
  `openai→openai_api_key`, `brave→brave_api_key`, `google_api→google_api_key`,
  `google_maps→google_maps_api_key`, and **leave `google_oauth` as-is** (it's the OAuth kind, its
  `type` is already `google_oauth`). Do this as an idempotent `UPDATE` in `db.py` schema init
  (guarded so it only rewrites known legacy values), so a redeploy self-heals without manual SQL.
- Injection (§4.3) then matches deterministically on `type`, and the declared `env_var` names the
  variable the agent reads it under.

**Blast radius:** the only writers of legacy `provider` values are the credentials router
(under our control) and the OAuth callback (`google_oauth`, unchanged). The email/geo agents read
`google_oauth` via the injected bundle — unchanged. Ship the migration + router change together.

### 4.6 P4c tests
- required + missing → blocked with consent prompt; no dispatch.
- required + stored + granted → proceeds; injected creds scoped to that agent only.
- optional + missing → proceeds.
- grant persists; revoke removes it; re-call re-prompts.
- agent with empty `required_credentials` → unchanged behavior (email compat carve-out).

---

## 5. P4d — Skeleton context seam + Settings UI

### 5.1 Skeleton seam (closes Gap 2, §0.4)
- Extend `AgentHandler.handle_structured` contract to an **optional** 3rd param:
  ```python
  async def handle_structured(self, user_input, files=[], context: dict | None = None) -> dict
  ```
  `context` carries `{"credentials": {...}, "user_id": ...}`.
- `HandlerExecutor.execute` ([handler_executor.py:60](../agent_skeleton/handler_executor.py#L60)):
  extract credentials from the inbound request metadata (from `RequestContext` — **verify at impl
  time** whether the a2a-sdk exposes `MessageSendParams.metadata` on the context; that's where the
  planner puts them per §0.4, adapters.py:162), build `context`, and inspect the handler signature
  (`inspect.signature`, precedent at [runtime.py:439](../planner_agent/main_agent/runtime.py#L439))
  — pass `context` only if the handler accepts it. **Existing 2-arg handlers keep working.**
- **Never log or persist `context`** (it holds raw secrets). Add a test asserting no secret hits logs.

### 5.2 Settings UI (planner web)
New Settings tab in [`planner_agent/main_agent/web/index.html`](../planner_agent/main_agent/web/index.html):
- List/add/update/delete stored keys (drives `/me/credentials/*`); never display secret values.
- List per-agent grants with revoke (drives `/me/grants/*`).

### 5.3 P4d tests
- 3-arg handler receives `context.credentials`; 2-arg handler still runs (signature inspection).
- `context` never appears in logs.
- Settings endpoints wired (light integration test with `FAKE_DB` where possible).

---

## 6. refbot note (unchanged from parent §8.6)
On the cluster refbot needs only `OPENAI_*` pointing at vLLM — **operator env, not a credential**
(D2). refbot can ship with **zero** declared credentials. The credential machinery matters for
agents calling **user-owned external APIs**. So refbot is not a P4 test vehicle; build a tiny
throwaway custom agent that declares (say) `openai_api_key` to exercise the full path.

---

## 7. Cross-cutting risks
- **DB requirement:** P4 is inert without `REGISTRATION_DB_DSN` + `CREDENTIALS_ENCRYPTION_KEY`.
  **Both are set in the cluster `deployment/.env`** (confirmed by maintainer 2026-07-02); still
  worth a line in `deployment/README.md`. Locally, tests use `FAKE_DB`/no-DSN paths.
- **Email compatibility:** the unscoped google_oauth bundle currently reaches email; the scoping
  carve-out (§4.3) must preserve it until the specialist-migration workstream lands.
- **Namespace (§4.5):** LOCKED (Option A). Ship the router change + legacy-value migration together.
- **No live-card fetch:** declarations only propagate on (re)publish; changing an agent's declared
  creds requires a re-publish (same constraint as skills).

## 8. Suggested exit gate (Track 1, analogous to D15)
End-to-end on the cluster: register a custom agent declaring `openai_api_key` (required) →
approve/build/spawn → planner discovery shows the requirement → call it with **no** key stored →
blocked with consent → add key in Settings + grant → re-call → agent receives the key via
`context` and returns a result → revoke in Settings → next call blocked again.

---

# Track 2 — Auth restructuring

Grounded in a direct read of the auth surface (§0) and
[`PLANNER_MULTIUSER_HARDENING_PLAN.md`](PLANNER_MULTIUSER_HARDENING_PLAN.md). Motivation
(maintainer, 2026-07-02): today the site **auto-signs-you-in with no buffer**, admin is a
**single shared token that's open when unset**, and the planner **shares one global conversation
context across all users** (data mixing). Track 2 fixes all three.

## 9. P4e — SSO login landing page (no more silent auto-sign-in)

**Today:** `GET /` serves the chat UI directly ([server.py:174](../planner_agent/main_agent/server.py#L174));
Apache/Shibboleth protects the whole app, so hitting any URL silently completes SSO and drops you
straight into chat. There is no landing/login screen.

**Target:** a public landing page at `/` with product framing + a single **"Sign in with WashU"**
button; the chat UI moves behind an authenticated route (e.g. `/app`). Logout returns to `/`.

**Work:**
1. **Apache/Shibboleth config (deployment):** make `/` (and static assets for the landing page)
   **public** (`require shibboleth` off / `ShibRequestSetting requireSession false`), and require a
   session only on the protected route(s) (`/app`, `/messages*`, `/me/*`, `/auth/*`, `/admin/*`).
   The "Sign in" button links to `/Shibboleth.sso/Login?target=/app` (or just `/app`, which triggers
   the SP). This is the load-bearing change — without it `/` can't be anonymous. Document in
   `deployment/README.md`; coordinate with whoever owns the Apache vhost.
2. **Planner serving:** split the single-page UI — serve a lightweight **public landing** at `/`
   (no identity needed) and the **chat UI** at `/app` behind `Depends(get_current_user)`. Reuse the
   existing single-file `web/index.html` for `/app`; add a small `landing.html`.
3. **Logout:** the existing `/auth/logout` ([auth_router.py:39](../planner_agent/main_agent/auth_router.py#L39))
   already redirects through `/Shibboleth.sso/Logout?return=/` — point `return` at the landing page.
4. **Local dev without Shibboleth:** the landing page's sign-in also needs to work behind the
   admin/dev path (§10) so the app is usable off-cluster.

**Note (SP-dependent):** whether `/` can be truly anonymous depends on the Apache SP config, which
lives outside this repo. If the SP can't be configured for mixed public/protected paths, fallback
is a planner-served interstitial: `/` returns the landing page for everyone, and only the "Sign in"
click navigates to the protected `/app`. Confirm the SP setup as **step 0** of P4e.

**P4e tests:** anonymous `GET /` → landing (200, no identity needed); `GET /app` without identity →
SSO redirect / 401; after sign-in → chat UI; logout → landing.

## 10. P4f — Admin via SSO + email allowlist (retire the shared token)

**Today:** `MAIN_AGENT_ADMIN_TOKEN` shared secret via `X-Admin-Token`; **open if unset**
([server.py:106–113](../planner_agent/main_agent/server.py#L106)); `/admin/login` returns the token
to the browser. Gates all `/admin/*` + the registration approver.

**Target (locked decision):** **admin = an authenticated WashU user whose email ∈ `ADMIN_EMAILS`.**
No separate admin password; the admin role is derived from the same Shibboleth identity.

**Work:**
1. New helper (in `auth.py`): `is_admin(user) -> bool` = `user["email"] in _admin_emails()` where
   `_admin_emails()` parses `ADMIN_EMAILS` (comma-separated). Add `require_admin` FastAPI dependency
   = `get_current_user` + `is_admin` else **403**.
2. Replace `_require_admin`/`X-Admin-Token` on every `/admin/*` route
   ([server.py:352…536](../planner_agent/main_agent/server.py#L352)) and the registration approver
   endpoints (`get_handler_source`, `/handler/download`, approve/reject proxy) with `require_admin`.
3. Remove `POST /admin/login` and the open-if-unset behavior. **Fail closed:** empty `ADMIN_EMAILS`
   → no admins (not "everyone").
4. **Frontend:** the approver panel and admin views (in `web/index.html`) stop sending
   `X-Admin-Token` and just rely on the authenticated session; hide admin UI when `/me` reports a
   non-admin. Add `is_admin` to the `/me` response.
5. **Local dev:** admin needs to work without Shibboleth — piggyback on the §10.3 dev-identity path
   (a dev flag that injects a fixed admin email), so a local `ADMIN_EMAILS` entry matches.

**P4f tests:** allowlisted user → `/admin/*` 200; authenticated non-allowlisted → 403; anonymous →
401; `ADMIN_EMAILS` empty → all admin routes 403; `/me` reports `is_admin` correctly.

## 11. P4g — Multi-user data separation (fold in the hardening plan)

Adopt [`PLANNER_MULTIUSER_HARDENING_PLAN.md`](PLANNER_MULTIUSER_HARDENING_PLAN.md) **H1–H3** into
this PR series (H4 only if the H1 follow-up audit finds a real shared-state race). Summary of what
lands here (see that doc for evidence + file:line):
- **H1 / F1 — per-user context keying (Critical C1):** `runtime._open_context` currently ignores
  its `context_id` arg and always mutates one global `server_context_id`
  ([store.py:11](../planner_agent/main_agent/state/store.py#L11),
  [runtime.py:818](../planner_agent/main_agent/runtime.py#L818)) — **all users share one thread.**
  Honor `context_id`, bind each context to its `user_id`, assert the loaded context's `user_id`
  matches the requester, retire the single-slot global. *First step: confirm `context_id`
  provenance from the UI/request.*
- **H2 / F2 — auth + ownership on data endpoints (Critical C2):** `GET /sessions`,
  `GET`/`DELETE /conversations/{context_id}` ([server.py:482–496](../planner_agent/main_agent/server.py#L482))
  take a plain `user_id` param / have no ownership check. Add `Depends(get_current_user)`, derive
  `user_id` from the principal, enforce ownership (403/404 on mismatch).
- **H3 / F3 — fail closed on missing identity (High C3):** replace the silent `demo-user` fallback
  ([server.py:588](../planner_agent/main_agent/server.py#L588)) with **401** when no identity is
  present and the DB is configured; keep `demo-user` only behind an explicit `PLANNER_ALLOW_ANON=1`
  dev flag. This is the **dev-identity path** P4e/P4f also rely on for local use.

**Interaction with Track 1:** credential isolation is *already* per-user-correct (§0.4 / hardening
plan "confirmed safe"), so P4g doesn't change credentials — but F3's fail-closed identity makes the
`user_id` that credential injection keys on trustworthy, so **land P4g (at least F3) before or with
P4c/P4d.**

**P4g tests:** two interleaved identities never see each other's `conversation_history` /
`current_goal` / context; user B reading/deleting A's `context_id` → 403/404; `GET /sessions`
returns only the caller's and ignores any `user_id` param; no-identity + DB + dev-flag-off → 401;
single-user regression unchanged.

## 12. Track 2 exit gate
On a Shibboleth-enabled deploy: anonymous `/` shows the landing page; sign-in lands on `/app` as
the SSO identity; a second concurrent user's chat stays fully separate; an allowlisted email can
reach the approver while a non-allowlisted authenticated user gets 403; logout returns to `/`.
