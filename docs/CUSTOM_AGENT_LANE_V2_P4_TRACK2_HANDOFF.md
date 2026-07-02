# P4 Track 2 — Handoff (Auth Restructuring)

**For:** a fresh session picking up **Track 2** (P4e/P4f/P4g) of
[`CUSTOM_AGENT_LANE_V2_P4_PLAN.md`](CUSTOM_AGENT_LANE_V2_P4_PLAN.md).
**Branch:** `feature/custom-agent-credentials-and-auth`.
**Date:** 2026-07-02.

---

## 1. Where things stand

**Track 1 — Per-user credentials (P4a–P4d): COMPLETE and cluster-validated.**
The full path works end-to-end on the ENGR cluster: register a custom agent declaring
`openai_api_key` → discover → **block** (no key) → add key in Settings → **consent** →
**grant** → **scoped injection** → the agent reads the key from `context` → **revoke** → blocked again.

- **P4a** (committed): declaration + catalog + card carrier (`capabilities.extensions`).
- **P4b** (committed): planner reads the extension into `DiscoveredAgent.required_credentials`.
- **P4c** (⚠️ **uncommitted, in working tree**): grants table, consent/enforcement, scoped injection, grants API.
- **P4d** (⚠️ **uncommitted, in working tree**): skeleton `context` seam + Settings UI (keys + grants).
- Plus two cluster-found fixes (uncommitted): `/confirmations` made credential-aware + single-card
  consent render; and the LLM no longer sees credential requirements (it was soliciting API keys in chat).

**Track 2 — Auth restructuring (P4e/f/g): NOT STARTED.** This is your work.

> **First action:** commit Track 1 (see §2), then start Track 2. Track 1 is validated but
> uncommitted; don't build Track 2 on an uncommitted base.

---

## 2. Commit Track 1 first (suggested grouping)

Whole-file grouping (no `git add -p` needed — each file lands in exactly one commit).
**Do NOT commit** `docs/ADS_POSTGRES_MIGRATION_PLAN.md` or `docs/personal_notes.md` (pre-existing, unrelated;
`personal_notes.md` probably shouldn't be in the repo at all).

1. `feat(planner): per-user credential grants — store, catalog, gate, API (P4c)`
   - `planner_agent/main_agent/credential_gate.py` (new)
   - `planner_agent/main_agent/credential_catalog.py` (new)
   - `planner_agent/main_agent/db.py`
   - `planner_agent/main_agent/credentials_router.py`
2. `feat(planner): credential enforcement, consent + scoped injection in the runtime (P4c)`
   - `planner_agent/main_agent/runtime.py` (enforcement/consent/scoping + `_agent_full_card` no longer leaks creds)
   - `planner_agent/main_agent/tools.py` (system-prompt rule: never solicit secrets)
   - `planner_agent/main_agent/server.py` (gate wiring, `auth_user_id` inject, `/confirmations` credential-aware, grants router)
3. `feat(planner): Settings UI for API keys + agent permissions, single-card consent (P4d)`
   - `planner_agent/main_agent/web/index.html`
4. `feat(skeleton): inject per-user credentials into handler context (P4d)`
   - `agent_skeleton/base.py`, `agent_skeleton/handler_executor.py`
5. `test: P4c/P4d credential enforcement, discovery, context seam`
   - `planner_agent/tests/test_credential_enforcement.py` (new)
   - `registration_service/tests/test_credential_context.py` (new)
   - `planner_agent/tests/test_credential_discovery.py` (modified)
6. `docs: mark P4 Track 1 done; note custom-agent cold-start on fresh deploy`
   - `docs/CUSTOM_AGENT_LANE_V2_P4_PLAN.md`, `deployment/README.md`, this handoff file

(Commits 2 and 3 each bundle a little "core + cluster-fix" because those changes share files;
splitting further would need `git add -p`. Fine to keep as-is.)

**Validate before committing:**
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 planner_agent/.venv/bin/python -m pytest planner_agent/tests -q
FAKE_DB=1 PYTHONPATH=. registration_service/.venv/bin/python -m pytest registration_service/tests -q
```
Expected: planner **39 passed + 6 pre-existing failures** (see §5); registration **96 passed**.

---

## 3. Track 2 scope (from the plan §9–§11)

| Sub-phase | Deliverable |
|---|---|
| **P4e** | SSO login **landing page** — public `/`, chat UI behind `/app`; logout → `/`. Needs Apache/Shib config for mixed public/protected paths. |
| **P4f** | **Admin via SSO + `ADMIN_EMAILS` allowlist** — retire the shared `X-Admin-Token`; fail closed (empty allowlist → no admins). |
| **P4g** | **Multi-user data separation** — fold in [`PLANNER_MULTIUSER_HARDENING_PLAN.md`](PLANNER_MULTIUSER_HARDENING_PLAN.md) H1–H3. |

**Recommended sequencing (from the plan):** land **P4g/F3 (fail-closed identity) and P4e early** — they
harden the identity floor everything else sits on. P4f depends on P4e's SSO/dev-identity path.

---

## 4. Ground truth learned this session (read before touching Track 2)

These were verified against the code while building Track 1 and directly affect Track 2.

### 4.1 The global-context bug (C1 / P4g-H1) is REAL and load-bearing
- `runtime._open_context` ([runtime.py:1048](../planner_agent/main_agent/runtime.py#L1048)) **ignores its
  `context_id` argument** and always returns one global slot via
  `store.get_server_context()` / `save_server_context` ([state/store.py:17-24](../planner_agent/main_agent/state/store.py#L17)).
  **All users/conversations share one context and one conversation history.**
- **Why it hasn't broken credentials:** pending confirmations are keyed by `context_id`
  (`store.pending_confirmations[context_id]`), and the browser echoes back the server's returned
  `context_id`, so single-user consent resume matches. It is **not multi-user safe** — two users
  interleaving will collide on the global context. P4g/H1 must honor `context_id`, bind each context
  to its `user_id`, and assert ownership.
- Practical note: the consent/confirmation resume paths (`_resume_confirmation`,
  `_resume_credential_consent`) depend on `get_pending_confirmation(context_id)` matching. When you fix
  the global context, re-verify both resume paths still find their pending state.

### 4.2 Identity + fail-closed (P4g-F3) is owed
- `auth.get_shib_email` ([auth.py:19](../planner_agent/main_agent/auth.py#L19)) reads `mail → eppn → remote-user`;
  `_incoming_message_from_request` falls back to **`demo-user`** when no header is present
  ([server.py:615, 649](../planner_agent/main_agent/server.py#L615)).
- **Credential/grant injection keys on the DB user UUID**, passed from the server via
  `metadata["auth_user_id"]` (set in `_inject_credentials`, [server.py](../planner_agent/main_agent/server.py)).
  Both `/messages` and `/confirmations` now call `_inject_credentials` (a P4c fix). So identity flows
  correctly **when a Shib header is present**; with no header it silently becomes `demo-user`, which would
  misattribute/leak credentials across anonymous callers. **F3 (401 when identity missing + DB configured,
  behind a `PLANNER_ALLOW_ANON=1` dev flag) should land before this is exposed multi-user.**
- On the cluster this is masked because Apache/Shibboleth always injects a header — but the header is
  spoofable if the planner port is reached directly (bypassing Apache). See CLAUDE.md §9.

### 4.3 Admin today (P4f target)
- `MAIN_AGENT_ADMIN_TOKEN` via `X-Admin-Token`; **open if unset** (`_require_admin` / `_check_admin_token`,
  [server.py:115-131](../planner_agent/main_agent/server.py#L115)). `POST /admin/login` returns the token.
- Gated routes: `/admin/*` ([server.py:372, 404, 418, 479, 537, 545, 554](../planner_agent/main_agent/server.py#L372))
  and the registration approver endpoints (in `registration_service/server.py`, `_check_admin_token`).
- P4f: add `is_admin(user)` + `require_admin` dependency in `auth.py`, parse `ADMIN_EMAILS`, replace every
  `_require_admin`, remove `/admin/login` + open-if-unset, add `is_admin` to `/me`, and stop the frontend
  sending `X-Admin-Token`.

### 4.4 Data endpoints lacking auth/ownership (P4g-H2)
- `GET /sessions` ([server.py:501](../planner_agent/main_agent/server.py#L501)) takes a plain `user_id="demo-user"` query param.
- `GET`/`DELETE /conversations/{context_id}` ([server.py:505, 515](../planner_agent/main_agent/server.py#L505)) have no ownership check.
- P4g/H2: add `Depends(get_current_user)`, derive `user_id` from the principal, enforce ownership (403/404 on mismatch).

### 4.5 Serving split (P4e)
- `GET /` currently serves the chat UI directly. P4e: public landing at `/`, chat behind `/app` +
  `Depends(get_current_user)`; logout (`auth_router.py`) already redirects through
  `/Shibboleth.sso/Logout?return=/`. The load-bearing external dependency is the **Apache SP config**
  (mixed public/protected paths) — confirm that as step 0; fallback is a planner-served interstitial.

---

## 5. Known-noise: 6 pre-existing planner test failures
`planner_agent/tests` has **6 failing tests unrelated to P4** (they fail on a clean checkout — verified via
`git stash`). Symptom: a directory test gets `coding-agent` where `weather-agent` was expected → likely a
stale card-path fixture. A background task is filed (`task_8627ad82`). **Don't let these block Track 2**, and
don't "fix" them by touching P4 code. If you fix them, do it in a separate commit.

---

## 6. Key files for Track 2

| File | Role in Track 2 |
|---|---|
| `planner_agent/main_agent/auth.py` | Add `is_admin`, `require_admin`; F3 fail-closed identity |
| `planner_agent/main_agent/server.py` | `/` vs `/app` split (P4e), replace `_require_admin` (P4f), auth+ownership on `/sessions` + `/conversations` (P4g-H2), retire `demo-user` fallback (P4g-F3), add `is_admin` to `/me` |
| `planner_agent/main_agent/runtime.py` + `state/store.py` | Per-user context keying (P4g-H1) — retire the single global slot |
| `planner_agent/main_agent/auth_router.py` | Logout `return` → landing |
| `planner_agent/main_agent/web/index.html` + new `landing.html` | Landing page + hide admin UI for non-admins |
| `deployment/` (Apache/Shib vhost, README) | Public `/` + protected routes; document |
| `docs/PLANNER_MULTIUSER_HARDENING_PLAN.md` | The H1–H3 source P4g folds in |

**Exit gate (plan §12):** anonymous `/` shows landing; sign-in lands on `/app` as the SSO identity; a second
concurrent user's chat stays fully separate; an allowlisted email reaches the approver while a
non-allowlisted authenticated user gets 403; logout returns to `/`.

---

## 7. Post-Track-2 cleanup (from plan §0.6)
`AUTH_PLAN.md` referenced by CLAUDE.md §8 **does not exist**. After Track 2, refresh CLAUDE.md §8 and either
write `AUTH_PLAN.md` or drop the links. Also: Google OAuth token refresh **is** implemented (contrary to
CLAUDE.md §8's "not done" list) — correct that too.
