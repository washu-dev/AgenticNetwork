# Bug Reporting & Agent-Creation Feedback — Implementation Plan

**Status:** proposed (not yet implemented)
**Date:** 2026-07-16
**Scope:** add two user-facing reporting paths to the deployed system, both landing as GitHub issues.

---

## 1. Goal

Give users two low-friction ways to send signal back to the dev team:

1. **Bug report** — "something is broken" from the main planner chat UI, any time.
2. **Agent-creation feedback** — how the agent creation flow went, from the registration UI.

Both **create a GitHub issue** in the (private) `washu-dev/AgenticNetwork` repo. There is
**no** local persistence or admin viewer — GitHub is the single destination.

---

## 2. Why GitHub, why one backend

- **GitHub as destination** puts reports where devs already triage; no admin UI to build/maintain.
- **One backend endpoint in the planner.** The registration UI is already reverse-proxied
  through the planner (`/register/{path}`, `planner_agent/main_agent/server.py:162`), so both
  forms POST to a single new **`POST /issues`** planner endpoint. Only one service holds the
  GitHub token and needs egress to `api.github.com`. (Egress confirmed available on the ENGR
  Slurm node; repo is private, so auto-captured context is safe to include.)

### Explicitly out of scope
- The existing in-memory `/feedback` 👍/👎 thumbs (`server.py:347`, `_feedback_log` deque at
  `server.py:110`) stays **as-is**. It is ephemeral by design and unrelated to these two asks.
- No Postgres `bug_reports` table, no admin viewer, no email notifications, no safety-net fallback.

---

## 3. Architecture

```
Planner chat UI ──POST /issues──┐
 (Report a bug button)          │
                                ▼
Registration UI ──/register/... proxy──▶  Planner  POST /issues  ──▶  GitHub REST API
 (creation feedback form)                 (holds token, builds       POST /repos/washu-dev/
                                           issue body, labels)        AgenticNetwork/issues
                                                │
                                                └──▶ returns { ok, issue_url } to the UI
```

---

## 4. Backend — planner `POST /issues`

**File:** `planner_agent/main_agent/server.py` (route) + a new helper module
`planner_agent/main_agent/github_issues.py` (the API call, isolated for testability).

### Request model (new Pydantic model in `server.py`)
```python
class IssueRequest(BaseModel):
    kind: str                       # "bug" | "agent_creation_feedback"
    summary: str                    # one-line -> issue title
    description: str                # user free text -> issue body
    # bug context (optional)
    context_id: str | None = None
    # agent-creation context (optional)
    step: str | None = None         # draft|validate|submit|approved|rejected
    agent_id: str | None = None
```

### Handler behavior
1. Resolve reporter identity via `get_shib_email()` (`main_agent/auth.py`). Include in body,
   **not** as GitHub author (author is always the bot token).
2. Build labels: `["user-submitted", "bug-report" | "agent-creation-feedback"]`.
3. Build the issue body (see §6), assembling auto-context server-side.
4. Call `github_issues.create_issue(title, body, labels)`.
5. On success return `{ "ok": True, "issue_url": <html_url> }`.
6. On failure (missing token / non-2xx / network error) return HTTP 502 with a clear
   message; log server-side with `logger.exception`. **No silent loss, no local fallback.**

### `github_issues.py`
- Reads `GITHUB_ISSUES_TOKEN` and `GITHUB_ISSUES_REPO` (default `washu-dev/AgenticNetwork`)
  from env. Missing token → raise a typed error the handler maps to 502.
- `POST https://api.github.com/repos/{repo}/issues` with
  `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`.
- Uses the async HTTP client already in the planner deps (httpx). Short timeout (~10s).

---

## 5. Auto-captured context (bugs)

Assembled **server-side** from what the user can't easily describe:

| Field | Source |
|---|---|
| reporter email | `get_shib_email()` |
| timestamp (UTC) | `time.gmtime()` |
| `context_id` | from request |
| last tool trace | snapshot of `_request_log` entries for this `context_id` (prompt, `routed_to`, `latency_ms`, `state`, timestamp) — see `server.py:238` |
| agent versions | from `directory.search_agents()` (name + version) |

**Truncation rule:** include routing/latency/version metadata in full; truncate any long raw
message/prompt text to a bounded length and mark it `…(truncated)`. Keeps issues readable and
avoids dumping large conversation bodies even into a private repo.

For **agent-creation feedback**, context is just: reporter email, timestamp, `step`, and
`agent_id` (if a draft exists).

---

## 6. Issue format

- **Title:** `summary` (fallback: first ~60 chars of `description`).
- **Body:**
  ```
  <user description>

  <details><summary>Auto-captured context</summary>

  - Reporter: <email>
  - Time: <utc>
  - Kind: bug | agent_creation_feedback
  - context_id / step / agent_id: ...

  Recent trace:
  | time | prompt | routed_to | latency_ms | state |
  ...
  </details>
  ```

---

## 7. Frontend

### 7a. Planner chat UI — "Report a bug"
**File:** `planner_agent/main_agent/web/index.html` (single-file UI).
- Persistent small button/link in the header or footer.
- Opens a modal: **summary** (text input) + **description** (textarea). Auto-context is added
  by the server, not the form — nothing for the user to fill.
- The UI already knows the active `context_id`; include it in the POST.
- POST to `/issues` with `kind: "bug"`. On success show the returned `issue_url` as a link;
  on failure show the error text.

### 7b. Registration UI — creation feedback
**File:** `registration_service/static/index.html`.
- Show the feedback prompt at the end of the create flow **and** on failure/abandonment of a
  step. Capture `step` and draft `agent_id` from the UI's current state.
- POST to the planner via the existing proxy (`/register/...` → resolve the correct path so
  the request reaches the planner's `/issues`), `kind: "agent_creation_feedback"`.
  - Confirm the proxy direction during implementation: registration static assets are served
    by the registration service, but the browser loads them under the planner origin when
    proxied — so a same-origin `POST /issues` from that page should reach the planner. Verify
    and adjust the path if the registration UI is also served standalone on `:8091`.

---

## 8. Configuration

New env vars (document in `deployment/README.md` and the relevant `.env.example`):

| Var | Purpose | Notes |
|---|---|---|
| `GITHUB_ISSUES_TOKEN` | fine-grained PAT / GitHub App token with **Issues: write** on the repo | store as a secret, same handling as `CREDENTIALS_ENCRYPTION_KEY` |
| `GITHUB_ISSUES_REPO` | target repo | default `washu-dev/AgenticNetwork` |

If `GITHUB_ISSUES_TOKEN` is unset, `/issues` returns 502 with a clear "reporting not
configured" message (fail loud, never silent).

---

## 9. Testing

- **Unit** (`planner_agent/tests`): mock the HTTP client; assert issue title/body/labels are
  built correctly for both `kind`s, that reporter email is injected, and that trace text is
  truncated. Assert 502 + logged error when token missing / GitHub returns non-2xx.
- **No network in tests** — inject a fake `create_issue` (same pattern as the fake ADS/LLM
  clients used elsewhere).
- **Manual (Tier A/B):** click the bug button, confirm a real issue appears with the right
  labels and a readable context block; submit creation feedback and confirm labeling.

---

## 10. Open items to confirm during implementation
1. Exact proxy path for the registration form → planner `/issues` (§7b).
2. Which httpx client instance to reuse in the planner (avoid opening a new pool per request).
3. Whether to also expose `/issues` behind the same auth as other authenticated planner routes
   (it should require a logged-in Shibboleth identity so reporter email is always present).

---

## 11. Implementation status (2026-07-16) — DONE

All five steps implemented and verified. Resolutions to §10:
1. The registration form POSTs to `/issues` on the **current origin**; `API_PREFIX` resolves to
   `/register` when proxied, confirming same-origin reach to the planner. Verified live: the POST
   hit the planner (its 502 message + a `kind=agent_creation_feedback` planner log line), not the
   registration service.
2. `github_issues.create_issue` opens a short-lived `httpx.AsyncClient` per call (10s timeout) —
   fine for a low-volume, user-initiated action; no shared pool needed.
3. `/issues` uses `_resolve_shib_user_id` — the reporter email is always the SSO identity (or
   `demo-user` in dev/anon mode). Any authenticated user can file (not admin-gated, by design).

**Deviation from plan:** `DiscoveredAgent` has no version field, so the bug context lists
**connected agent names** rather than `name@version`.

**Files changed:**
- `planner_agent/main_agent/github_issues.py` (new) — GitHub REST call, `GitHubIssueError`, `is_configured`.
- `planner_agent/main_agent/server.py` — `IssueRequest`, `build_issue_payload`, `POST /issues`, `context_id` added to `_request_log`.
- `planner_agent/main_agent/web/index.html` — "Report a bug" button + modal + JS.
- `registration_service/static/index.html` — creation-feedback panel + wiring in `submitRegistration`.
- `deployment/{secrets.env.example,.env.example,submit.sbatch,README.md}`, `planner_agent/.env.example` — config/docs.
- `planner_agent/tests/test_issues_endpoint.py` (new) — 8 tests.

**Tests:** planner `76 passed`; registration `97 passed`.

---

## 12. Staged deployment testing (do this before prod)

The one real unknown is **the token + egress working from a cluster node** — everything else is
verified locally. Test in stages; do not add the prod secret until the test instance proves it out.

### Stage A — local API/UI check (laptop, no cluster)
Already verified without a token (forms render, 502 on submit). To verify a **real issue is created**,
point at a throwaway repo first:
```bash
# Use a fine-grained PAT with Issues:write on a TEST repo you own.
export GITHUB_ISSUES_TOKEN=github_pat_...
export GITHUB_ISSUES_REPO=<your-user>/issues-sandbox
OPENAI_API_KEY=dummy MAIN_AGENT_LLM_ENABLED=true PYTHONPATH=. \
  planner_agent/.venv/bin/python -m planner_agent.scripts.run_server --fake-ads --port 8090
# then, in another shell:
curl -s http://localhost:8090/issues -H 'Content-Type: application/json' \
  -d '{"kind":"bug","summary":"smoke test","description":"ignore me"}' | jq
```
Expect `{"ok":true,"issue_url":"…"}` and a new issue in the sandbox repo with labels
`user-submitted`, `bug-report`. Also submit via the UI at `http://localhost:8090/app` (bug button)
and `http://localhost:8090/register/` (creation feedback, needs the registration service running too).

### Stage B — cluster **test** instance (proves egress + real token)
1. Put the real token in your **per-user** secrets file (not the shared prod one):
   `~/.config/agenticnetwork/secrets.env` → `GITHUB_ISSUES_TOKEN=…`
   and set `GITHUB_ISSUES_REPO=washu-dev/AgenticNetwork` in your `.env` (or rely on the default).
2. Deploy the isolated test instance: `deployment/deploy.sh test`.
3. Tunnel to the test planner (see `deployment/README.md` "Accessing a test instance"), then:
   - `curl` the `/issues` endpoint through the tunnel (as in Stage A) — **this is the egress test**.
   - Confirm the issue appears in `washu-dev/AgenticNetwork` with correct labels and a readable
     auto-context block (reporter, trace, agent names).
   - Submit creation feedback via the proxied `/register/` UI and confirm the `agent-creation-feedback`
     issue.
4. If the curl fails with a 502 whose log says "could not reach GitHub", egress is blocked → stop and
   revisit (the whole GitHub approach depends on it).

### Stage C — prod
Only after Stage B is green:
1. Add `GITHUB_ISSUES_TOKEN` to the **shared** prod secrets file
   (`/engrfs/project/di2/secrets/agenticnetwork.env`, mode 0640) and `GITHUB_ISSUES_REPO` to prod `.env`.
2. Redeploy prod (`deployment/deploy.sh` / normal submit path).
3. Post-deploy smoke: file one bug via the live UI, confirm the issue, then close it.

> No DB migration and no `submit.sbatch` logic change are required — the new vars ride the existing
> `set -a; source secrets.env` path into the planner container.
