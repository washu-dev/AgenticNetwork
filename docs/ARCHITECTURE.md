# Architecture

How AgenticNetwork fits together, one level below [`CLAUDE.md`](../CLAUDE.md) §1–§2. Read that
first for the plain-English overview and the run tiers; this doc is the design deep-dive.

## The shape of the system

AgenticNetwork is an **"internet of agents"**: anyone can add an agent and anyone can use it.
Independent LLM-powered HTTP services publish themselves to a shared directory, and a router
(today, the planner) finds the right ones there and calls them over a standard agent-to-agent
protocol.

- **The chat app** — `chat_app/`. The web service people sign in to and chat in: sign-in, the
  chat page, saved conversations, each user's encrypted keys, admin pages and the `/register/*`
  pass-through to the registration service. Overview: [`chat_app/README.md`](../chat_app/README.md).
- **One orchestrator** — the **planner**, the LLM loop inside the chat app
  (`chat_app/planner/`). The brain you talk to. It has two jobs today: **routing** (it searches
  the directory and delegates whole tasks to the chosen agents) and **responding** (it writes
  the reply the user sees from their results). It does no domain work itself, and it is
  deliberately **agent-agnostic**: adding
  an agent to the directory makes it usable with zero planner code changes. Deep dive:
  [PLANNER.md](PLANNER.md).
- **Specialist agents** — each wraps one capability behind an HTTP endpoint (email, coding,
  intelligence, geospatial, disaster-response, scout). They live in the separate
  `agent-directory` repo. New ones are registered through the registration service or built
  on the `agent_skeleton` agent runtime — [BUILDING_AGENTS.md](BUILDING_AGENTS.md).
- **A directory (ADS)** — AGNTCY's `dir` daemon; this repo's client code and operator tooling are
  in `agentnet/`, the one shared library the chat app and the registration service use for
  directory and A2A calls. The registry where agents publish a card so others can find them by
  skill. Deep dive: [AGENT_DIRECTORY.md](AGENT_DIRECTORY.md).
- **A registration service** — `registration_service/`. A sign-up wizard + API where agent
  authors register agents (including user-uploaded custom ones). It also builds and runs those
  agents and publishes their cards to the directory. Deep dive:
  [REGISTRATION_SERVICE.md](REGISTRATION_SERVICE.md).

## The three contracts

Everything interoperates through three contracts, and nothing else — see [`CLAUDE.md`](../CLAUDE.md)
§1 for the table (Agent Card / ADS / A2A). This is what lets agents be written in any
language/framework and still cooperate: because agents share only these wire contracts — never
Python imports at runtime — each can have any dependencies, any Python version, and run on any
machine or container. That's why agents deliberately use *different* environments and may even
disagree on library versions; a built agent image keeps the `a2a-sdk` it was built with. The two
services in this repo share code in-process (`agentnet/`), so they are developed in one venv on
one `a2a-sdk` version (0.3.2). See [`CLAUDE.md`](../CLAUDE.md) §5.

## Request lifecycle (end to end)

```
You (browser / curl)
   │  POST /messages
   ▼
Chat app FastAPI  (chat_app/server.py)
   │  PlannerRuntime.handle_message  (chat_app/planner/runtime.py)
   ▼
LLM tool loop  (llm_client.py) — the model sees only 5 primitives (tools.py):
   list_agents · describe_agent · call_agent · ask_user · final_answer
   │
   ├── list_agents / describe_agent ──▶ DirectoryClient ──▶ ADS  (dirctl, or in-process fake)
   │
   └── call_agent ──▶ resolve agent · check policy · inject scoped credentials
                       └──▶ A2A request (HTTP/JSON-RPC) ──▶ specialist agent's endpoint
                                                              │  its own executor / LLM loop
                                                              ▼
                                                            text summary + DataPart artifact
   │
   ▼
Planner normalizes the result → LLM emits final_answer → server serializes the
response (with a tool trace) back to the caller.
```

1. A user message hits `POST /messages` (or `/messages/stream` for SSE) in
   `chat_app/server.py`.
2. `PlannerRuntime` (`chat_app/planner/runtime.py`) runs a bounded tool loop. The LLM
   (`chat_app/planner/llm_client.py`, OpenAI Chat Completions — vLLM-compatible) sees only the **5
   primitives** in `chat_app/planner/tools.py`.
3. `list_agents` / `describe_agent` query ADS via `DirectoryClient`
   (`agentnet/directory/`), which shells out to `dirctl` — or uses an in-process fake in
   `--fake-ads` mode.
4. `call_agent` resolves the agent, checks policy (write-skill gating), scopes + injects the
   user's granted credentials, and dispatches the task over A2A (`agentnet/a2a/`).
5. The specialist runs its own executor / LLM loop and returns a text summary + a
   structured-data `DataPart` artifact (the machine-readable channel the planner reads).
6. The planner normalizes the result; the LLM emits `final_answer`; the server serializes the
   response (with a tool trace) back to the UI/caller.

The full internals of steps 2–6 (context/session persistence, policy, credential enforcement,
dedup) are in [PLANNER.md](PLANNER.md); identity + credentials in [AUTH.md](AUTH.md).

## The load-bearing subtlety: stored vs. routed

ADS records are both **stored** (content-addressed; `dirctl search`) **and** separately
**routed** (a routing index; `dirctl routing list/search`). A record can be **stored but not
routed** — in which case it is **invisible to live discovery** even though it exists. The
directory backend deliberately raises `DirectoryUnavailable` rather than silently falling back
to storage search, so this failure mode surfaces instead of hiding. The entire
`agentnet/scripts/` suite exists to detect and repair it. Full mechanics:
[AGENT_DIRECTORY.md](AGENT_DIRECTORY.md); rationale: [DECISIONS.md](DECISIONS.md) D1.

## Realities worth knowing up front

- **One shared library here, copies in the agents repo.** This repo's A2A + directory code is
  one package, `agentnet/`. The `agent-directory` repo has its own divergent copies
  (`_vendor_v2/` trees in coding/intelligence, scout's own, and others; see
  [`SECURITY.md`](../SECURITY.md) §3). Library pins drift. When
  fixing a protocol/security bug, check whether the fix must be replicated across copies.
- **Authentication is at the chat app only.** Only the chat app (`:8090`) is behind WashU
  Shibboleth; the registration pages are reached through it.
  Specialist ports and ADS/vLLM have no auth and bind `0.0.0.0` — safe only on a trusted node.
  See [AUTH.md](AUTH.md) and [`SECURITY.md`](../SECURITY.md).
- **Two credential-sourcing models** coexist (operator env-var vs. per-user) — see
  [AUTH.md](AUTH.md) and [DECISIONS.md](DECISIONS.md) D6.
- **The chat app's durable state is in Postgres when `DATABASE_URL` is set** (users,
  conversation history, pending confirmations), so it can run as multiple processes. Without a DB
  (dev stack, tests) they live only in that process's memory and are lost on restart. See
  [PLANNER.md](PLANNER.md) and [DECISIONS.md](DECISIONS.md) D11.
