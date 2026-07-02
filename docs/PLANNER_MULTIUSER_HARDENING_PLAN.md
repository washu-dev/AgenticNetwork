# Planner Multi-User Hardening Plan

**Status:** Scheduled — **folded into Track 2 (P4g)** of
[`CUSTOM_AGENT_LANE_V2_P4_PLAN.md`](CUSTOM_AGENT_LANE_V2_P4_PLAN.md) (2026-07-02). Problem verified
in code; H1–H3 land in the P4 credentials-and-auth branch. This doc remains the detailed evidence +
fix reference for that sub-phase.
**Scope:** `planner_agent/main_agent/` (state store, runtime context handling, HTTP endpoints, auth)
**Trigger:** The planner (`:8090`) has only ever been tested by a single user. A code review
(2026-07-01) found it is **not safe for concurrent multiple users** — it shares one global
conversation context across everyone, and several endpoints expose other users' data.
**Relationship to other work:** Now co-scheduled with the P4 credential work and the auth
restructuring (SSO landing page + admin-via-allowlist) in the same PR series — the auth surface is
shared. (Note: the `AUTH_PLAN.md` referenced by CLAUDE.md §8 does **not** exist in the repo; treat
code as truth.) Still the **higher-urgency** item: live data mixing/exposure, not a missing feature.

---

## 1. Verdict

**Not multi-user-ready.** With ≥2 users, conversations mix into one shared thread and users
can read/delete each other's data. The good news: the credential subsystem is already
per-user-correct, and the fixes are contained (no re-architecture).

## 2. Findings

### Confirmed (read directly in code)

| # | Severity | Issue | Evidence |
|---|---|---|---|
| C1 | **Critical** | **Single global conversation context.** `InMemoryStateStore` has one `server_context_id` slot; `runtime._open_context` **ignores its `context_id` argument** and always fetches/mutates that one context — overwriting `user_id`/`current_goal` and appending to a shared `conversation_history`. Every user shares one thread; bleed + interleaving occur **even with Shibboleth working**. | [store.py:11,17-24](../planner_agent/main_agent/state/store.py#L11), [runtime.py:818-828](../planner_agent/main_agent/runtime.py#L818) |
| C2 | **Critical** | **Unauthenticated / no-ownership endpoints.** `GET /sessions` takes a plain `user_id` query param (default `demo-user`); `GET`/`DELETE /conversations/{context_id}` have no auth and no ownership check. Anyone can enumerate/read/delete others' data. | [server.py:482](../planner_agent/main_agent/server.py#L482), [server.py:486](../planner_agent/main_agent/server.py#L486), [server.py:496](../planner_agent/main_agent/server.py#L496) |
| C3 | **High** | **Silent `demo-user` fallback.** `_incoming_message_from_request` catches any `get_shib_email` failure and defaults to `"demo-user"`, so without Shibboleth all requests collapse to one identity. Bites in dev / any non-Shibboleth deploy. | [server.py:588-596](../planner_agent/main_agent/server.py#L588), [auth.py:19-28](../planner_agent/main_agent/auth.py#L19) |

### Confirmed safe (no action needed)

- **Per-user credential isolation** — `get_current_user(request)` → `fetch_user_credentials(user["id"])` with `WHERE user_id = $1`. No cross-user key leakage. ([server.py:573](../planner_agent/main_agent/server.py#L573), [db.py:81](../planner_agent/main_agent/db.py#L81))
- **Persisted history** (`conversation_store`, SQLite) is keyed by `context_id` correctly — only the *in-memory working context* is global. Once C1 is fixed, persistence needs no change.
- **Identity threading** is per-request when Shibboleth headers are present.

### Reported by the audit, needs verification before acting

- **In-memory mutation races** on `contexts` / `pending_confirmations` / `_session_index`.
  Note: the planner runs a single-threaded asyncio loop, so individual dict ops don't corrupt;
  the real risk is *read-modify-write spanning `await`s* on a **shared** object — which C1's
  per-user keying largely dissolves. Verify whether any remaining read-modify-write across
  awaits touches shared state after C1. (audit: `server.py:100-104`, `state/store.py`)
- **`_session_index` concurrent append** loss — likely a non-issue after C1 + single-threaded
  loop, but confirm.

## 3. Fixes

### F1 — Key the working context by user/session (fixes C1)
- Requests already pass a `context_id` into `_open_context` — **honor it** instead of calling
  `get_server_context()`. Look up `self.store.contexts[context_id]`; create a fresh context
  (bound to `user_id`) if absent; never reuse another user's.
- **First implementation step:** confirm `context_id` provenance — where the request/UI supplies
  it (request body / session cookie / generated per conversation) and that it's stable per user
  session. If the client doesn't send a per-user id, derive/allocate one server-side keyed to
  the authenticated `user_id` (+ session).
- Remove or repurpose `server_context_id` (the single-slot global) so nothing can fall back to
  a shared context. Add an assertion that a loaded context's `user_id` matches the requester.

### F2 — Auth + ownership on data endpoints (fixes C2)
- Add `Depends(get_current_user)` to `GET /sessions`, `GET /conversations/{context_id}`,
  `DELETE /conversations/{context_id}`.
- Derive `user_id` from the authenticated principal — **not** a query param.
- Enforce **ownership**: the requesting user must own the `context_id`/session before read/delete
  (store an owner `user_id` alongside each context/conversation and check it).

### F3 — Fail closed on missing identity (fixes C3)
- Replace the silent `demo-user` fallback with: honor the existing **admin-login** path
  ([auth.py]/admin login) for local dev, otherwise **401** when no identity is present.
- Keep `demo-user` only behind an explicit dev flag (e.g. `PLANNER_ALLOW_ANON=1`), never the
  default in a deployment with `REGISTRATION_DB_DSN` set.

### F4 — Concurrency hygiene (after F1, only if verification confirms need)
- If any shared read-modify-write across `await`s remains, guard with an `asyncio.Lock` or move
  that state to the DB. Prefer per-context serialization (two concurrent requests for the *same*
  session queue) over global locks.

## 4. Phasing

| Phase | Deliverable | Fixes | Notes |
|---|---|---|---|
| **H1** | Per-user/session context keying | F1 | The core fix; unblocks safe concurrent chat. Verify `context_id` provenance first. |
| **H2** | Auth + ownership on the 3 endpoints | F2 | Small, high-value; closes the data-exposure holes. |
| **H3** | Fail-closed identity | F3 | Guards non-Shibboleth deploys. |
| **H4** | Concurrency audit follow-up | F4 | Only if H1 leaves a real shared-state race. |

H1+H2 are the minimum to let real users on. H1 before H2 (ownership checks need per-user
contexts to exist).

## 5. Testing
- **Concurrency test (new):** simulate two users (distinct identities) interleaving `/messages`;
  assert each sees only its own `conversation_history`, `current_goal`, and context_id — no bleed.
- **Ownership test:** user B calling `GET/DELETE /conversations/{A's context_id}` → 403/404;
  `GET /sessions` returns only the caller's sessions and ignores any `user_id` param.
- **Fail-closed test:** no Shibboleth headers + DB configured + dev flag off → 401 (not
  `demo-user`).
- **Regression:** single-user flow (with Shibboleth or admin login) unchanged end-to-end.

## 6. Caveats / out of scope
- `InMemoryStateStore` is **process-local**: fine for the single planner process today, but if
  the planner is ever horizontally scaled, working state must move to shared storage (DB/Redis).
  Note it; not required for correctness on one process.
- This plan does not touch specialist agents (no per-user state on the main agent) or the vLLM
  backend (shared throughput, not a correctness issue).
