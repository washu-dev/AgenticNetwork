# Architecture

How AgenticNetwork fits together, one level below [`CLAUDE.md`](../CLAUDE.md) §1–§2. Read that
first for the plain-English overview and the run tiers; this doc is the design deep-dive.

## The shape of the system

AgenticNetwork is an **"internet of agents"**: independent LLM-powered HTTP services that
discover each other through a shared directory and call each other over a standard
agent-to-agent protocol.

- **One orchestrator** — the **planner** (`planner_agent/`). The brain you talk to. It has
  *no domain skills of its own*; it discovers other agents and delegates whole tasks. It is
  deliberately **agent-agnostic**: adding an agent to the directory makes it usable with zero
  planner code changes. Deep dive: [PLANNER.md](PLANNER.md).
- **Specialist agents** — each wraps one capability behind an HTTP endpoint (email, coding,
  intelligence, geospatial, disaster-response, scout). They live in the separate
  `agent-directory` repo. New ones are built from the template — [BUILDING_AGENTS.md](BUILDING_AGENTS.md).
- **A directory (ADS)** — `agent_directory_service/`. The registry where agents publish a card
  so others can find them by skill. Deep dive: [AGENT_DIRECTORY.md](AGENT_DIRECTORY.md).
- **A registration service** — `registration_service/`. A web UI + API to register agents
  (including user-uploaded custom ones) into the directory. Deep dive:
  [REGISTRATION_SERVICE.md](REGISTRATION_SERVICE.md).

## The three contracts

Everything interoperates through three contracts, and nothing else — see [`CLAUDE.md`](../CLAUDE.md)
§1 for the table (Agent Card / ADS / A2A). This is what lets agents be written in any
language/framework and still cooperate: because they share only these wire contracts — never
Python imports at runtime — each can have any dependencies, any Python version, and run on any
machine or container. That's why the components deliberately use *different* environments and
even disagree on library versions (ADS pins `a2a-sdk==0.3.2`; the planner uses `0.3.26`). See
[`CLAUDE.md`](../CLAUDE.md) §5.

## Request lifecycle (end to end)

```
You (browser / curl)
   │  POST /messages
   ▼
Planner FastAPI  (server.py)
   │  MainAgentRuntime.handle_message  (runtime.py)
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
   `main_agent/server.py`.
2. `MainAgentRuntime` (`main_agent/runtime.py`) runs a bounded tool loop. The LLM
   (`main_agent/llm_client.py`, OpenAI Chat Completions — vLLM-compatible) sees only the **5
   primitives** in `main_agent/tools.py`.
3. `list_agents` / `describe_agent` query ADS via `DirectoryClient`
   (`main_agent/directory/`), which shells out to `dirctl` — or uses an in-process fake in
   `--fake-ads` mode.
4. `call_agent` resolves the agent, checks policy (write-skill gating), scopes + injects the
   user's granted credentials, and dispatches the task over A2A (`main_agent/a2a/`).
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
`agent_directory_service/scripts/` suite exists to detect and repair it. Full mechanics:
[AGENT_DIRECTORY.md](AGENT_DIRECTORY.md); rationale: [DECISIONS.md](DECISIONS.md) D1.

## Realities worth knowing up front

- **Heavy code duplication, not a shared library.** The A2A + directory protocol layer exists
  in ~5 divergent copies (canonical `agent_directory_service/services/`, `_vendor_v2/` trees in
  coding/intelligence, the more-evolved planner fork, scout's own). Library pins drift. When
  fixing a protocol/security bug, check whether the fix must be replicated across copies.
- **Authentication is planner-only.** Only the planner (`:8090`) is behind WashU Shibboleth.
  Specialist ports and ADS/vLLM have no auth and bind `0.0.0.0` — safe only on a trusted node.
  See [AUTH.md](AUTH.md) and [`SECURITY.md`](../SECURITY.md).
- **Two credential-sourcing models** coexist (operator env-var vs. per-user) — see
  [AUTH.md](AUTH.md) and [DECISIONS.md](DECISIONS.md) D6.
- **The planner's durable state is in Postgres** (conversation history + pending
  confirmations), so it can run as multiple processes; see [PLANNER.md](PLANNER.md) and
  [DECISIONS.md](DECISIONS.md) D11.
