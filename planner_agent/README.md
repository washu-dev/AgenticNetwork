# Planner Agent

A re-write of the main / PI agent for the Internet-of-Agents demo.

> **New here?** See [`HANDOVER.md`](./HANDOVER.md) for the 30-second onboarding.
> One-shot environment self-check: `planner_agent/scripts/doctor.sh`.

The planner agent is standalone: its ADS publishing/discovery helpers, A2A
transport, state models, state store, and sample Agent Cards live under
`planner_agent/`. The deprecated v1/v2 PI implementations are not imported by
the active planner runtime.

## What was wrong with the old PI Agent

The v2 `pi_agent/main.py` was ~1700 lines and had a lot of hardcoded
skill-specific logic (`_extract_available_slots`, `_extract_draft_email`,
`_extract_weather_forecast`, `_extract_situation_summary`,
`_extract_coding_output`, …). Whenever someone added a new ADS-registered
agent with a new skill, the PI agent had no idea how to surface its outputs
without new Python code. The deterministic router was a giant keyword table
hard-coded for the known skills.

## Design of the new main agent

The new main agent is **agent-agnostic**. Domain logic lives in the agents
ADS publishes; the main agent is a thin tool-loop coordinator.

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

### LLM reasoning is surfaced

For reasoning models (gpt-5.x, o-series), each tool step is preceded by a
`{"tool": "reasoning", ...}` entry in `tool_trace`. The chat UI renders it
as a blue italic block ("thinking → … chose: list_agents") so the user can
see *why* the LLM picked the next tool, when the model emits a reasoning channel.

## Layout

```
planner_agent/
├── HANDOVER.md             # 30-second teammate onboarding
├── README.md               # this file
├── .env.example            # config template (copy to .env.local)
├── main_agent/
│   ├── config.py           # MainAgentConfig from env vars
│   ├── models.py           # planner-local A2A, ADS, policy, and state models
│   ├── a2a/                # planner-local A2A client, adapters, HTTP transport
│   ├── directory/          # planner-local fake/real ADS client + card converter
│   ├── state/              # planner-local in-memory state store
│   ├── tools.py            # 5 tool schemas + system prompt
│   ├── llm_client.py       # OpenAI Responses client + ScriptedLLMClient
│   ├── runtime.py          # tool loop, ADS, A2A, memory, policy, dedup
│   ├── server.py           # FastAPI: /messages /confirmations /agents /health /
│   └── web/index.html      # chat UI (sidebar + tool-trace + reasoning blocks)
├── scripts/
│   ├── start.sh            # auto-loads .env.local, starts uvicorn
│   ├── doctor.sh           # one-shot self-check (env, dirctl, ADS, server, OpenAI, tests)
│   └── run_server.py       # python entrypoint (called by start.sh)
└── tests/                  # tests run with mocked LLM + ADS
```

Standalone planner infrastructure:

- `main_agent/models.py` — `DiscoveredAgent`, `A2A*`, `PIContext`, policy records, etc.
- `main_agent/directory/` — `DirectoryClient` (fake ADS + real ADS via `dirctl`)
- `main_agent/a2a/` — `A2AClient`, SDK adapters, and HTTP streaming transport
- `main_agent/state/store.py` — in-memory state store

## Running

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
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  planner_agent/.venv/bin/python -m pytest planner_agent/tests -q
```

27 tests cover: list/describe/call agent flows, write-skill confirmation +
resume, negative confirmation, undiscovered agent error handling, follow-up
context across turns, dedup of repeated calls, retry after a failed A2A call,
empty `task_input` augmentation from user text, reasoning-block in trace,
per-turn (not per-client) chain state, single-dispatch compound workflow
guarding, and a dynamic agent (Disaster Agent) added without any change to the
main agent code.

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
  --card email_agent/email_agent.card.json \
  --card coding_agent/coding_agent.card.json
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
  --card email_agent/email_agent.card.json
```

Open `http://localhost:8090/` for the chat UI, or POST to `/messages`:

```bash
curl -s http://localhost:8090/messages \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"demo","channel":"api","text":"What agents are available?"}' | jq
```

`GET /agents` returns the raw ADS listing for inspection.

### 4. Real ADS (cluster or tunnel)

In the cluster the planner container connects to ADS automatically via
`ADS_SERVER_ADDR`. For local testing against a tunneled cluster ADS (see
`agent_directory_service/README.md`), point the planner at it directly — agents
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
agent_directory_service/run_ads.sh -publish email_agent/email_agent.card.json
```

The same `/messages` and `/agents` endpoints work; they just hit real ADS
through `dirctl` instead of the in-process fake.

## Environment variables

| Variable                       | Default       | Purpose                                                       |
| ------------------------------ | ------------- | ------------------------------------------------------------- |
| `OPENAI_API_KEY`               | —             | Required when LLM enabled                                     |
| `MAIN_AGENT_LLM_ENABLED`       | `true`        | Set false for purely scripted (test) runs                     |
| `MAIN_AGENT_LLM_PROVIDER`      | `openai`      | Only `openai` is supported today                              |
| `MAIN_AGENT_LLM_MODEL`         | `gpt-4o-mini` | OpenAI model id (we ship `.env.example` with `gpt-5.1`)       |
| `MAIN_AGENT_MAX_OUTPUT_TOKENS` | `1500`        | Per-turn output cap. Bump to ≥4000 for reasoning models       |
| `MAIN_AGENT_MAX_STEPS`         | `10`          | Max tool-loop iterations per user message                     |
| `MAIN_AGENT_TIMEOUT_SECONDS`   | `60`          | OpenAI call timeout                                           |
| `MAIN_AGENT_POLICY_MODE`       | `permissive`  | `permissive` allows write skills; `strict` requires confirmation |
| `MAIN_AGENT_HISTORY_LIMIT`     | `20`          | Last-N user/assistant turns sent to LLM                       |
| `AGENT_NETWORK_FAKE_ADS`       | `false`       | Use in-process fake ADS                                       |
| `ADS_BACKEND`                  | `cli`         | `cli` (uses `dirctl`) or `sdk`                                |
| `ADS_SERVER_ADDR`              | —             | e.g. `localhost:8888`                                         |
| `MAIN_AGENT_PORT`              | `8090`        | HTTP port (avoids collision with v2's 8080)                   |
| `PYTHON_BIN`                   | auto-detected | Optional interpreter override for wrapper scripts             |

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
- **Per-turn OpenAI chain ids** — `previous_response_id` and `last_call_id`
  live on `TurnState`, not on the client instance, so concurrent
  `/messages` requests can't interleave each other's response chains.

## What is NOT included

- No skill-specific output extractors (`_extract_available_slots`,
  `_extract_draft_email`, `_extract_weather_forecast`, etc. from v2). The
  structured outputs are returned to the LLM as-is and rendered through
  `final_answer`.
- No deterministic keyword router (v2's `router.py`). The LLM picks the
  agent.
- No legacy planner mode. Tool loop only.
- No persistence. `InMemoryStateStore` resets on restart. Add SQLite if you
  need it.

If a future user prompt needs a domain-specific renderer (a chart, a map,
etc.), add a renderer in the front-end on top of `structured_outputs` —
not in the main agent.
