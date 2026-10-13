# Planner (Orchestrator) Internals

The planner (`planner_agent/`) is the brain you talk to. It has no domain skills — it discovers
agents via ADS and delegates whole tasks over A2A. This is the code-level view; see
[ARCHITECTURE.md](ARCHITECTURE.md) for the system picture and [AUTH.md](AUTH.md) for
identity/credentials.

## Entry points (`main_agent/server.py`)

FastAPI app. Key routes:
- `GET /` → public landing (`web/landing.html`); `GET /app` → the chat UI, hard-gated (D7).
- `POST /messages` (JSON) / `POST /messages/stream` (SSE) → the main turn.
- `GET /agents` → raw directory listing; `GET /health` (liveness); `GET /ready` (200/503 with
  per-dependency `checks`: directory, db, llm).
- `GET /me` → `{id, email, is_admin}`; creates the user on first call (in memory without a DB).
- `POST /confirmations` → resume a pending write/consent prompt (owner only, else 403).
- `POST /issues` → files a user bug report or agent-creation feedback as a GitHub issue (labels
  `user-submitted` + `bug-report`/`agent-creation-feedback`; needs `GITHUB_ISSUES_TOKEN`, target
  `GITHUB_ISSUES_REPO`; returns 502 when unconfigured or GitHub fails, never drops silently).
  Code: `main_agent/github_issues.py`; decision D12.
- `GET /sessions`, `GET`/`DELETE /conversations/{context_id}` → per-user data (ownership-enforced).
- `/admin/*` → admin dashboard data (allowlist-gated); `/register/{path}` → proxy to the
  registration service (admin subpaths gated; `internal/*` refused with 404).
- Auth/credential routers (`auth_router`, `credentials_router`, grants) mount only when
  `REGISTRATION_DB_DSN` is set.

A request's identity is resolved once by `_resolve_shib_user_id` (fail-closed — see
[AUTH.md](AUTH.md) §1). Credentials are fetched + scoped by `_inject_credentials` and passed via
`metadata`.

## The tool loop (`main_agent/runtime.py`)

`MainAgentRuntime.handle_message(user_id, channel, text, context_id, metadata, …)` is the core.
It runs a **bounded** loop (`config.max_steps`) in which the LLM (`llm_client.py`, OpenAI Chat
Completions) may call only the **5 primitives** defined in `main_agent/tools.py`:

| Tool | Effect |
|---|---|
| `list_agents` | ADS search → candidate agents |
| `describe_agent` | full card for one agent (`_agent_full_card`) |
| `call_agent` | dispatch a task to an agent over A2A (`_tool_call_agent`) |
| `ask_user` | pause and ask the user a question |
| `final_answer` | end the turn with a response |

The system prompt (tools.py) forbids the model from soliciting secrets; credential handling is
entirely out-of-band (§ below).

### `call_agent` path (`_tool_call_agent`)
1. Resolve the agent from ADS (`DirectoryClient`).
2. Evaluate **policy** (`_evaluate_policy`): in `strict` mode a write skill (by
   `capability.action_type == "write"` or a write-skill hint) → `requires_confirmation`. Default
   `policy_mode` is `permissive` (no gating). A no-hint call is not gated even in strict — a known
   gap (see [`SECURITY.md`](../SECURITY.md)).
3. Enforce **credentials** (`_enforce_credentials`) — declare/grant/consent (see [AUTH.md](AUTH.md)
   §4). Missing-required → block; ungranted → consent prompt; scoped injection via
   `_scope_credentials`.
4. Build + send the A2A request (`_build_a2a_request` → `a2a/`); normalize the result.
5. **Dedup:** completed calls are fingerprinted per turn so an identical request isn't re-dispatched.

## Context, sessions & persistence

This is where the multi-user hardening lives (see [AUTH.md](AUTH.md) §5, [DECISIONS.md](DECISIONS.md)
D9–D11).

- **`_open_context(user_id, channel, text, context_id)`** (async) — per-user context keying. Looks
  up the caller-supplied `context_id`; if found, asserts `context.user_id == user_id` else raises
  `ContextOwnershipError` (→ 403). On an in-memory miss it **rehydrates from Postgres**
  (`_rehydrate_context` → `conversation_store`), so a conversation resumes on any process or after a
  restart. Unknown/never-persisted id → a fresh context bound to the caller (reusing the id). The
  old single global `server_context_id` slot was removed.
- **`InMemoryStateStore`** (`state/store.py`) — the per-process cache: `contexts`,
  `pending_confirmations`, and the write-only `invocations` audit.
- **`conversation_store.py`** (Postgres, `conversation_messages`) — durable per-conversation
  history, owner-stamped, write-through cached, UPSERT-per-turn (keeps `created_at` stable so
  `/sessions` timestamps are real). No-op without a DB.
- **`pending_store.py`** (Postgres, `pending_confirmations`) — durable pending write/consent state.
  The runtime helpers `_save_pending` / `_get_pending` / `_clear_pending` write-through to the
  in-memory cache and Postgres and rehydrate on a miss — **best-effort** (a DB error degrades to
  the cache, never fails the turn).
- `GET /sessions` derives from the persistence layer (Postgres when configured, else in-memory
  contexts) keyed on the authenticated principal — no `user_id` query param.

## Confirmation & consent resume

A write-skill confirmation or a credential-consent prompt saves a `pending_confirmations` entry and
returns a terminal "reply yes/no" (or "Allow"). The resume paths read it back:
- `handle_message` detects an affirmative/negative reply on a `context_id` with pending state.
- `_resume_confirmation` (write skill) / `_resume_credential_consent` (records the grant, then
  dispatches) / `_cancel_confirmation` (async; clears pending).
- `POST /confirmations` (the button path) first checks that the caller owns the conversation
  (403 otherwise), then re-injects credentials and routes by `pending.kind`.

Because pending state is durable (D11), these resume correctly across processes/restarts.

## Directory access (`main_agent/directory/`)

`DirectoryClient` wraps a real (`dirctl`-shelling) or fake (in-process, `--fake-ads`) backend. The
planner's converter fork (`card_oasf_converter.py`) additionally parses declared credentials off the
card (`_required_credentials_from_card`, the read path) and accepts both `demo-v2`/`demo-v3` records.
Every `dirctl` call is killed after `DIRCTL_TIMEOUT_SECONDS` (default 60) and surfaces as
`DirectoryUnavailable`; the `/ready` directory check uses a fixed 4 s limit. See
[AGENT_DIRECTORY.md](AGENT_DIRECTORY.md) for ADS mechanics.

## LLM configuration

`llm_client.py` uses OpenAI **Chat Completions** (vLLM-compatible) — the planner was migrated off
the Responses API. Point it at a vLLM server with `OPENAI_BASE_URL` + a dummy key + the served model
name. Full matrix in [`CLAUDE.md`](../CLAUDE.md) §7.

## Testing

`planner_agent/tests` run with no network/DB/OpenAI key (ScriptedLLMClient + fake ADS):
```bash
planner_agent/.venv/bin/python -m pytest planner_agent/tests -q -p no:cacheprovider
```

## Key files
| Path | Role |
|---|---|
| `main_agent/server.py` | FastAPI surface, identity, credential injection, data endpoints |
| `main_agent/runtime.py` | tool loop, dispatch, policy, credential enforcement, context/pending persistence |
| `main_agent/tools.py` | the 5 tool schemas + system prompt |
| `main_agent/llm_client.py` | Chat Completions client |
| `main_agent/state/store.py` | in-process cache (contexts, pending, invocations) |
| `main_agent/conversation_store.py` / `pending_store.py` | Postgres durable state |
| `main_agent/db.py` | Postgres pool + schema |
| `main_agent/directory/` | ADS client + backends + converter fork |
| `main_agent/a2a/` | A2A request build/dispatch/adapters |
| `main_agent/web/index.html` / `landing.html` | chat UI / public landing |
