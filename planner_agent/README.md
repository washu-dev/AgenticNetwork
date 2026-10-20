# Planner Agent

The chat orchestrator: an LLM tool loop that discovers agents in ADS and calls them over A2A.

> One-shot environment self-check: `planner_agent/scripts/doctor.sh`.

The planner agent is standalone: its ADS publishing/discovery helpers, A2A
transport, state models and state store live under `planner_agent/`
(test fixture Agent Cards: `planner_agent/tests/fixtures/agent_cards/`).

## Design

The planner is **agent-agnostic**. Domain logic lives in the agents
ADS publishes; the planner is a thin tool-loop coordinator.

The LLM only sees five safe primitives:

| Tool             | Purpose                                                                 |
| ---------------- | ----------------------------------------------------------------------- |
| `list_agents`    | Enumerate every agent in ADS with a one-line summary                    |
| `describe_agent` | Full skill catalog (ids, descriptions, examples) for one agent          |
| `call_agent`     | Dispatch the whole task to one discovered agent through A2A             |
| `ask_user`       | Stop the turn and ask a clarifying question                             |
| `final_answer`   | Final response to the user                                              |

ADS discovery, A2A transport, conversation memory, policy gating, and
confirmation state all live in Python — never in the model's hands. The LLM
cannot see endpoint URLs or credentials.

This design directly answers the front-end test prompts:

| User prompt                                  | What the LLM does                                                 |
| -------------------------------------------- | ----------------------------------------------------------------- |
| "what agents are available now?"             | `list_agents` → `final_answer`                                    |
| "what can the disaster agent do?"            | `list_agents` → `describe_agent` → `final_answer`                 |
| "draft me a tornado plan"                    | `list_agents` → `call_agent(disaster, whole task)` → `final_answer` |
| "send this plan to xxx by email"             | `list_agents` → `call_agent(email, whole task)` → `final_answer`  |
| "write me a python program"                  | `list_agents` → `call_agent(coding, whole task)` → `final_answer` |
| "write a program and email it"               | `list_agents` → one `call_agent` to a primary related agent; that agent handles peer A2A |

## Layout

```
planner_agent/
├── README.md               # this file
├── .env.example            # config template (copy to .env.local)
├── main_agent/
│   ├── config.py           # MainAgentConfig from env vars
│   ├── env.py              # setting names, with the old names as fallbacks
│   ├── models.py           # policy and state models; re-exports the wire models from agentnet
│   ├── state/              # planner-local in-memory state store (per process)
│   ├── tools.py            # 5 tool schemas + system prompt
│   ├── llm_client.py       # OpenAI Chat Completions client + ScriptedLLMClient
│   ├── runtime.py          # tool loop, ADS, A2A, memory, policy, dedup
│   ├── server.py           # FastAPI: /messages /confirmations /agents /health /ready /me /
│   ├── auth.py             # Shibboleth SSO identity + get_current_user dependency
│   ├── auth_router.py      # /auth/* — logout + Google OAuth connect flow
│   ├── credentials_router.py  # /me/credentials/* — per-user API-key store/list/delete
│   ├── credential_catalog.py  # canonical credential-type catalog
│   ├── credential_gate.py  # per-agent credential-grant enforcement
│   ├── crypto.py           # AES-256-GCM encrypt/decrypt for stored credentials
│   ├── db.py               # Postgres pool + schema (users, credentials, conversations, pending)
│   ├── conversation_store.py  # Postgres-backed conversation history
│   ├── pending_store.py    # Postgres-backed pending confirmations
│   └── web/                # index.html (chat UI) + landing.html (pre-login page)
├── scripts/
│   ├── start.sh            # auto-loads .env.local, starts uvicorn
│   ├── doctor.sh           # one-shot self-check (env, dirctl, ADS, server, OpenAI, tests)
│   └── run_server.py       # python entrypoint (called by start.sh)
└── tests/                  # tests run with mocked LLM + ADS
```

Standalone planner infrastructure:

- `main_agent/models.py` — `DiscoveredAgent`, `A2A*`, `PIContext`, policy records, etc.
- `agentnet/directory/` (repo root) — `DirectoryClient` (fake ADS + real ADS via `dirctl`)
- `agentnet/a2a/` (repo root) — `A2AClient`, SDK adapters, and HTTP streaming transport
- `main_agent/state/store.py` — in-memory state store

## Running

To run the whole system on a laptop (planner, registration service, a seed agent and
a mock LLM) with one command, use the dev stack instead: `scripts/stack up`
([`docs/DEVSTACK.md`](../docs/DEVSTACK.md)). The steps below run the planner alone.

### 1. Install

Run from the repository root:

```bash
cd /path/to/AgenticNetwork
python3 -m venv planner_agent/.venv
planner_agent/.venv/bin/python -m pip install --upgrade pip
planner_agent/.venv/bin/python -m pip install -r planner_agent/requirements.txt
planner_agent/.venv/bin/python -m pip check
```

The wrapper scripts auto-detect `planner_agent/.venv/bin/python`. To use a
different interpreter, set `PYTHON_BIN=/path/to/python`.

### 2. Tests (no network, no ADS, no OpenAI key)

```bash
cd /path/to/AgenticNetwork
planner_agent/.venv/bin/python -m pytest -q planner_agent/tests
```

The tests use a scripted LLM and the in-process fake ADS. They cover:
list/describe/call agent flows, write-skill confirmation + resume, negative
confirmation, undiscovered agent error handling, follow-up context across turns,
dedup of repeated calls, retry after a failed A2A call, empty `task_input`
augmentation from user text, per-turn (not per-client) chain state,
single-dispatch compound workflow guarding, and a dynamic agent (Disaster Agent)
added without any change to the planner code.

### 3. Local fake-ADS demo (no real ADS daemon needed)

First-time setup — copy the env template and put your OpenAI key in:

```bash
cd /path/to/AgenticNetwork
cp planner_agent/.env.example planner_agent/.env.local
$EDITOR planner_agent/.env.local      # set OPENAI_API_KEY=sk-...
```

`.env.local` is gitignored. Then start the server with the wrapper script
that auto-loads it:

```bash
planner_agent/scripts/start.sh --fake-ads \
  --card ../agent-directory/email_agent/email_agent.card.json \
  --card ../agent-directory/coding_agent/coding_agent.card.json
```

Use a different env file:

```bash
ENV_FILE=/path/to/custom.env planner_agent/scripts/start.sh --fake-ads
```

Skip the wrapper and export by hand:

```bash
export OPENAI_API_KEY=sk-...
export AGENT_NETWORK_FAKE_ADS=true
planner_agent/.venv/bin/python -m planner_agent.scripts.run_server \
  --fake-ads \
  --card ../agent-directory/email_agent/email_agent.card.json
```

The cards come from the sibling `agent-directory` checkout. Agents are discovered
from them but only answer if they are actually running.

Open `http://localhost:8090/app` for the chat UI, or POST to `/messages`:

```bash
curl -s http://localhost:8090/messages \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"demo","channel":"api","text":"What agents are available?"}' | jq
```

`GET /agents` returns the raw ADS listing for inspection. `GET /health` is a cheap
liveness check; `GET /ready` returns 200 or 503 with a per-dependency `checks` map
(`directory`, `db`, `llm`; each `ok`, `not_configured` or `error` with an `error`
string). `GET /me` returns `{id, email, is_admin}` and creates the user on first call.
An identity header (`mail`, `eppn`, `remote-user`) always wins over a body `user_id`.

### 4. Real ADS (cluster or tunnel)

In the cluster the planner container connects to ADS automatically via
`ADS_SERVER_ADDR`. For local testing against a tunneled cluster ADS (see
`agentnet/README.md`), point the planner at it directly — agents
will already be publishing their own cards:

```bash
export OPENAI_API_KEY=sk-...
export AGENT_NETWORK_FAKE_ADS=false
export ADS_BACKEND=cli
export ADS_SERVER_ADDR=localhost:8888
planner_agent/.venv/bin/python -m planner_agent.scripts.run_server
```

To manually publish a card (e.g. an agent crashed before publishing):

```bash
scripts/ads -publish ../agent-directory/email_agent/email_agent.card.json
```

The same `/messages` and `/agents` endpoints work; they just hit real ADS
through `dirctl` instead of the in-process fake.

## Environment variables

`PLANNER_*` settings tune the planner (the LLM loop that picks agents and calls
them). `CHAT_APP_*` settings tune the web server around it. Settings other
services share keep their own names.

**Old names still work.** Most of these settings used to start with
`MAIN_AGENT_` instead: `MAIN_AGENT_LLM_MODEL` is now `PLANNER_LLM_MODEL`, and
`MAIN_AGENT_PORT` is now `CHAT_APP_PORT`. Four don't follow that pattern:
`MAIN_AGENT_MAX_OUTPUT_TOKENS` is now `PLANNER_LLM_MAX_OUTPUT_TOKENS`,
`MAIN_AGENT_TIMEOUT_SECONDS` is now `PLANNER_LLM_TIMEOUT_SECONDS`,
`PLANNER_ALLOW_ANON` is now `CHAT_APP_ALLOW_ANON`, and `HEALTH_PROBE_SKIP_AGENTS`
is now `CHAT_APP_HEALTH_PROBE_SKIP_AGENTS`. When a new name is unset, the server
reads the old one and logs a warning naming the new one. If both are set to
different values, the new name wins, and that is logged too. The full list is in
`main_agent/env.py`.

| Variable                       | Default       | Purpose                                                       |
| ------------------------------ | ------------- | ------------------------------------------------------------- |
| `OPENAI_API_KEY`               | —             | Required when LLM enabled                                     |
| `OPENAI_BASE_URL`              | —             | OpenAI-compatible endpoint (mock, vLLM, Ollama); `/ready` probes `{base}/models` |
| `PLANNER_LLM_ENABLED`          | `true`        | Set false for purely scripted (test) runs                     |
| `PLANNER_LLM_PROVIDER`         | `openai`      | `openai` (any OpenAI-compatible endpoint) or `vertex` (Vertex AI, ADC token) |
| `PLANNER_LLM_MODEL`            | `gpt-4o-mini` | OpenAI model id, or your vLLM `--served-model-name`           |
| `PLANNER_LLM_MAX_OUTPUT_TOKENS` | `1500`       | Per-turn output cap. Bump to ≥4000 for reasoning models       |
| `PLANNER_LLM_TIMEOUT_SECONDS`  | `60`          | LLM call timeout                                              |
| `PLANNER_LLM_FALLBACK_PROVIDER` / `_BASE_URL` / `_MODEL` | — | Optional second LLM, tried when the first one fails to connect, times out or answers 5xx. A fallback provider needs a fallback model |
| `PLANNER_LLM_PRIMARY_TIMEOUT_SECONDS` | `20`   | Shorter timeout for the first LLM when a fallback is set, so a down LLM fails over quickly |
| `PLANNER_MAX_STEPS`            | `10`          | Max tool-loop iterations per user message                     |
| `PLANNER_POLICY_MODE`          | `permissive`  | `permissive` allows write skills; `strict` requires confirmation |
| `PLANNER_HISTORY_LIMIT`        | `20`          | Last-N user/assistant turns sent to LLM                       |
| `PLANNER_UNSCOPED_CREDENTIAL_AGENTS` | —       | Comma-separated agent ids that get the user's whole credential bundle (legacy agents that read credentials without declaring them; logged). Others get only declared + granted types |
| `A2A_HTTP_TIMEOUT_SECONDS`     | `30`          | Timeout for each call to an agent                             |
| `CHAT_APP_HOST`                | `0.0.0.0`     | Address the server listens on                                 |
| `CHAT_APP_PORT`                | `8090`        | HTTP port                                                     |
| `CHAT_APP_LOG_LEVEL`           | `INFO`        | Python log level                                              |
| `CHAT_APP_UPLOAD_DIR`          | `/tmp/planner_agent_uploads` | Where chat uploads are saved. The planner only sends agents files from inside this folder |
| `CHAT_APP_MAX_UPLOAD_BYTES`    | `52428800` (50 MiB) | Largest upload accepted (bigger ones get 413)           |
| `CHAT_APP_CARD_PATHS`          | —             | Comma-separated Agent Card files to pre-load (fake-ADS mode only; same as `--card`) |
| `CHAT_APP_ALLOW_ANON`          | off           | Exactly `1` lets requests without an identity header in as `demo-user` even with a database configured. For local dev and test instances only |
| `CHAT_APP_HEALTH_PROBE_SKIP_AGENTS` | —        | Comma-separated agent ids the background health check never calls (keeps scaled-to-zero agents asleep) |
| `REGISTRATION_SERVICE_URL`     | `http://localhost:8091` | Where the `/register/*` pages are forwarded          |
| `AGENT_NETWORK_FAKE_ADS`       | `false`       | Use in-process fake ADS                                       |
| `ADS_BACKEND`                  | `cli`         | `cli` (uses `dirctl`) or `sdk`                                |
| `ADS_SERVER_ADDR`              | —             | e.g. `localhost:8888`                                         |
| `DIRCTL_TIMEOUT_SECONDS`       | `60`          | Per-call limit on `dirctl`; a hung call is killed (`/ready` uses 4s) |
| `PYTHON_BIN`                   | auto-detected | Optional interpreter override for wrapper scripts             |
| `REGISTRATION_DB_DSN`          | —             | Postgres DSN for users, conversation history, credentials and pending confirmations. Unset: users live in memory |
| `CREDENTIALS_ENCRYPTION_KEY`   | —             | 32-byte base64 key for AES-256-GCM credential encryption      |
| `ADMIN_EMAILS`                 | —             | Comma-separated admin allowlist (matched against SSO identity); fail-closed |
| `GITHUB_ISSUES_TOKEN` / `_REPO` | —            | Token and target `owner/name` for `POST /issues`. No default repo: use a private one (issues carry the reporter's email, prompts and trace). Either unset → 502 |
| `GOOGLE_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | — | Google OAuth web client for per-user Google connect |

## Confirmation flow (when `policy_mode=strict`)

1. User: "Send an email to bob@example.com saying hi."
2. LLM: `list_agents` → `call_agent(email-agent, email/send, {to, subject, body})`.
3. Runtime detects `email/send` is a write skill, returns
   `confirmation_required=true` with a saved pending action.
4. User replies "yes" / "no":
   - `yes` resumes the saved A2A call with `confirmed=true` and appends the
     confirmation turn to the conversation history.
   - `no` clears the pending action and tells the user it was cancelled.

Affirmative tokens: `yes y confirm confirmed go ahead send send it ok okay`.
Negative tokens: `no n cancel stop abort do not send don't send`.

## Defensive layers

The runtime carries a few belt-and-braces protections so flaky LLM behavior
doesn't break the demo:

- **Lenient agent resolver** — `Disaster Response Agent`, `disaster-response-agent`,
  `DisasterResponseAgent`, and even `disaster` (substring) all resolve to the
  same agent record. The LLM doesn't have to be picky.
- **Per-turn dedup** — the same request fingerprint already completed in this
  turn returns a `skipped` observation instead of re-invoking A2A. Prevents
  duplicate emails when the LLM second-guesses itself.
- **Single-dispatch guard** — after one successful external-agent dispatch in a
  user turn, a second different dispatch is skipped. External agents are
  responsible for peer A2A coordination.
- **Retry after failure** — failed A2A calls do *not* count toward dedup, so
  the LLM can retry transient errors.
- **Email task-input augmentation** — if the LLM forgets to fill `to`,
  `subject`, or `body` on `email/*` skills, the runtime extracts them from
  the user text and conversation history before sending the A2A request.
- **Empty `final_answer` salvage** — if a reasoning model truncates output
  and emits `final_answer({})`, the runtime falls back to the most recent
  `call_agent` text so the user sees the agent's actual reply, not
  `(empty)`.
- **Per-turn tool-call state** — the tool-loop state (e.g. `last_call_id`, the
  Chat Completions `tool_call_id` the next tool result is attached to) lives on a
  per-turn `state` object, not on the client instance, so concurrent `/messages`
  requests can't interleave each other's tool-call chains.

## What is NOT included

- No skill-specific output extractors. Structured outputs are returned to the
  LLM as-is and rendered through `final_answer`.
- No deterministic keyword router. The LLM picks the agent.
- No durable working state: `InMemoryStateStore` lives in the process and resets
  on restart. With `REGISTRATION_DB_DSN` set, conversation history and pending
  confirmations also persist to Postgres (`conversation_store` /
  `pending_store` → `db.py`), keyed by SSO identity; without it they live only
  in memory.

If a future user prompt needs a domain-specific renderer (a chart, a map,
etc.), add a renderer in the front-end on top of `structured_outputs` —
not in the main agent.
