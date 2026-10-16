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
  `user-submitted` + `bug-report`/`agent-creation-feedback`; needs `GITHUB_ISSUES_TOKEN` and
  `GITHUB_ISSUES_REPO`, which has no default and must be a private repo because issues carry the
  reporter's email, prompts and trace; returns 502 when either is unset or GitHub fails, never
  drops silently).
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
   `_scope_credentials`: only declared + granted types (declared types only when there is no
   gate or auth identity), so an agent that declares none gets none. Agent ids in
   `PLANNER_UNSCOPED_CREDENTIAL_AGENTS` (comma-separated; legacy agents that read credentials
   without declaring them) get the whole bundle instead, logged as
   `credentials_unscoped_legacy_agent`.
4. Build + send the A2A request (`_build_a2a_request` → `a2a/`); normalize the result.
5. **Dedup:** completed calls are fingerprinted per turn so an identical request isn't re-dispatched.

## Context, sessions & persistence

This is where the multi-user hardening lives (see [AUTH.md](AUTH.md) §5, [DECISIONS.md](DECISIONS.md)
D9–D11).

- **`_open_context(user_id, channel, text, context_id)`** (async) — per-user context keying. Looks
  up the caller-supplied `context_id` with `_owned_context`, which raises `ContextOwnershipError`
  (→ 403) unless the caller owns it. The owner is the in-memory context's; on a miss it
  **rehydrates from Postgres** (`_rehydrate_context` → `conversation_store`), so a conversation
  resumes on any process or after a restart; with no saved history, the pending row's owner
  (`pending_store.owner`) still counts. An id with no owner anywhere → a fresh context bound to the
  caller (reusing the id). The old single global `server_context_id` slot was removed.
- **`InMemoryStateStore`** (`state/store.py`) — the per-process cache: `contexts`,
  `pending_confirmations`, and the write-only `invocations` audit.
- **`conversation_store.py`** (Postgres, `conversation_messages`) — durable per-conversation
  history, owner-stamped, write-through cached, UPSERT-per-turn (keeps `created_at` stable so
  `/sessions` timestamps are real). No-op without a DB.
- **`pending_store.py`** (Postgres, `pending_confirmations`) — durable pending write/consent
  state. The runtime helpers `_save_pending` / `_lookup_pending` (`_get_pending`) /
  `_claim_pending` / `_finish_pending` / `_clear_pending` write-through to the in-memory cache
  and Postgres — **best-effort** (a DB error degrades to the cache, never fails the turn, with
  one exception below). With the DB reachable, `_lookup_pending` reads the row on every lookup
  and drops a cached copy whose row is gone or claimed, so an action resumed or cancelled on one
  process is not run again from another process's cache. `_claim_pending` marks the row claimed
  under a fresh claim id (`claimed_at`, `claim_id`) with one `UPDATE` that matches only an
  unclaimed row still holding the prompt the answer read (`pending_store.claim`, comparing the
  whole payload), then drops the cached copy. Refresh, release and delete match that claim id, so
  they never touch another answer's claim or a newer prompt (saving a prompt clears any claim).
  While the agent call runs, the answer refreshes the claim every `CLAIM_REFRESH_SECONDS` (20 s;
  `pending_store.refresh`, each attempt bounded at `CLAIM_REFRESH_TIMEOUT_SECONDS`, 10 s; a
  failed attempt drops its connection, since asyncpg would wait for a reply a dead connection
  never sends before reusing it), because a claim reads as in progress only until
  `CLAIM_TTL_SECONDS` (2 minutes) after its last refresh, and agent calls can run far longer
  (registered agents up to an hour); a live claim expires only if three attempts in a row fail.
  `_finish_pending` deletes the row once the agent call or the cancel is done
  (`pending_store.finish`). `_claimed` wraps the refresh and the delete around the call. The
  delete runs to the end even if the answer is cancelled (closing the chat stream cancels it
  mid-call, and the server keeps cancelling it), and so does the release of a claim whose
  statement failed or was cancelled after the database may have applied it
  (`pending_store.release`). The claim is the one exception to best-effort: if that statement
  fails, the answer gets `pending_store_unavailable`, because running the cached copy would leave
  the row for a later answer to run again. A failed delete or release, a process that dies
  mid-call, or a claim statement the database applies only after its release (its connection
  dropped while the statement waited on a lock someone else held on the row), leaves the claim:
  it reads as in progress for up to 2 minutes, then as nothing pending, and it is never claimed
  again. The row stays until the conversation's next prompt replaces it or the conversation is
  deleted. Planners before this change (8ea4cfd and earlier) ignore `claimed_at`, so before
  rolling the planner back past this change, delete the claimed rows (`DELETE FROM
  pending_confirmations WHERE claimed_at IS NOT NULL`); no deployment runs two planner versions
  on one database at once.
- `GET /sessions` derives from the persistence layer (Postgres when configured, else in-memory
  contexts) keyed on the authenticated principal — no `user_id` query param.

## Confirmation & consent resume

A write-skill confirmation or a credential-consent prompt saves a `pending_confirmations` entry and
returns a terminal "reply yes/no" (or "Allow"). The resume paths read it back:
- `handle_message` detects an affirmative/negative reply on a `context_id` with pending state and
  first checks ownership with `_owned_context` (403 otherwise), because the resumed call carries
  the replying user's credentials and a consent records the grant under that user.
- `POST /confirmations` (the button path) runs the same check (403 otherwise), then re-injects
  credentials.
- Both then call `_answer_pending`, which routes by `pending.kind` to `_resume_confirmation`
  (write skill), `_resume_credential_consent` (dispatches, then records the grant) or
  `_cancel_confirmation`. Two answers can arrive together (a double click, a retry, a chat "yes"
  racing the button, replies on two processes); each answer runs at most once:
  - Within one process, one answer per conversation runs at a time (`_answering`); another gets
    `failed` with `errors: ["answer_in_progress"]` and leaves the action alone. With a database
    the claimed row stays until the answer is done, so an answer arriving on any other process
    meanwhile, by chat or button, gets `answer_in_progress` too. `handle_message` routes a bare
    yes or no to `_answer_pending` while a prompt is pending, a live claim holds it, or this
    process is answering, so a repeated "yes" never starts an LLM turn while the first answer
    runs.
  - Each handler first does what can fail (credential lookups, building the request), then
    claims the action (`_claim_this`) and calls the agent with nothing that can fail in between.
    A failure before the claim leaves the action pending; after the claim, only the agent call
    runs. Consent grants are recorded after the call: if that fails, the call has still run on
    the consent just given, and the owner is asked again next time.
  - The claim matches the prompt the answer read. Across processes the loser gets
    `no_pending`; a newer prompt that replaced the one read is not claimed and stays for its
    own answer.
  - Each prompt carries a `prompt_id` (a random hex id, returned in `pending_confirmation`).
    The confirm button sends it back (`POST /confirmations` takes an optional `prompt_id`); a
    press on a prompt that a newer one replaced gets `superseded` and runs or cancels nothing.
    A chat "yes" or "no" carries no id and answers the pending prompt, the latest one the chat
    showed. A request without a `prompt_id` (an older client), or a row saved without one (by
    an older planner), is answered as before.
  - A confirm on a process that doesn't know the conversation returns `unknown_context`, and a
    claim the database can't serve returns `pending_store_unavailable`; both leave the action
    pending. (A claim the database applied but couldn't confirm is released; if that release
    fails too, the prompt reads as in progress for up to 2 minutes, then as nothing pending, as
    above.)
- The prompt stores the request it asks about (`user_goal`); the resumed call sends that, not the
  conversation's current goal, which a later message may have changed.

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
