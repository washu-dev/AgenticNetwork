# Scout

Environment-centric OSINT (open-source intelligence) research agent. Given a region,
topic, or entity, Scout autonomously plans queries, pulls public web/news, and
returns a cited markdown brief. Designed to be deployed as a node on an
[AGNTCY](https://docs.agntcy.org/) / [A2A](https://a2a-protocol.org/) agent
network — discoverable through a shared directory and callable by other agents
over the JSON-RPC binding of A2A.

Scout is *environment*-centric: it surveys the geopolitical / economic /
infrastructure / public-sentiment information environment around a topic. It
does **not** do adversary tracking, target attribution, or operational planning
— those belong to a separate Intelligence agent.

## Architecture

Scout runs in three modes, all sharing the same ReAct loop:

| Mode | Command | Network | Use case |
|---|---|---|---|
| One-shot | `scout "your mission"` | none | Single research request, batch / pipe |
| REPL | `scout` | none | Interactive multi-turn research |
| Networked | `scout serve` | publishes to AGNTCY dir, listens for A2A calls, can call peers | Production deployment in a multi-agent network |

The core abstraction is the `Capability` interface ([scout/capabilities/base.py](scout/capabilities/base.py)):
the LLM doesn't distinguish between calling a local tool (Brave Search) and
calling a peer agent (over A2A). Both are `Capability` implementations with a
JSON-schema input and an async `invoke()` method. The ReAct loop dispatches
through a `CapabilityRegistry` and never imports adapters directly.

```
                 ┌──────────────────────────────────────┐
                 │     ScoutSession (ReAct loop)        │
                 │   plan → tool_call → observe → …     │
                 └────────┬──────────────────┬──────────┘
                          │                  │
                ┌─────────▼─────┐   ┌────────▼─────────┐
                │ BraveCapabili-│   │ A2APeerCapability │
                │ ties (web,    │   │ (one per peer    │
                │  news)        │   │  discovered in   │
                │               │   │  the directory)  │
                └───────────────┘   └────────┬─────────┘
                                             │ JSON-RPC over HTTP
                                             ▼
                                    other agents on the network
```

When `scout serve` starts, it:
1. Connects to the AGNTCY directory daemon
2. Cleans up stale records named `Scout` from previous runs
3. Discovers all A2A peers in the dir (via `MODULE_NAME=integration/a2a` query)
4. Registers each peer as an `A2APeerCapability` in the LLM's tool list
5. Starts a JSON-RPC HTTP server (uvicorn) on `SCOUT_HTTP_PORT`
6. Pushes its own OASF record to the dir + announces it on the routing layer

On graceful shutdown (Ctrl-C / Ctrl-D / EOF), Scout unpublishes + deletes its
own record from the dir.

## Quick start

### Prerequisites

- Python 3.12+
- An OpenAI API key (`OPENAI_API_KEY`)
- A Brave Search API key (`BRAVE_API_KEY`)
- For networked mode: access to an AGNTCY directory daemon (run locally via
  the bundled `docker-compose.yml`, or point at a shared/remote dir)

### Install

```bash
git clone <this-repo> scout
cd scout
python3.12 -m venv .venv
.venv/bin/pip install -e .

cp .env.example .env
# edit .env: fill in OPENAI_API_KEY and BRAVE_API_KEY
```

### Run (one-shot)

```bash
.venv/bin/scout "Summarize semiconductor export controls trends over the past 60 days." -v
```

Output is markdown to stdout (a four-section brief: Situation Summary / Trend
Observations / Sources / Confidence & Gaps), with `-v` adding iteration-level
trace to stderr. Use `--output brief.md` to write to file.

### Run (REPL)

```bash
.venv/bin/scout
```

Multi-turn chat. Earlier turns are visible in subsequent turns so follow-ups
("dig deeper into X") reuse prior context without re-searching.

### Run (networked / serve)

```bash
# 1. Bring up a local directory daemon (skip if you have a remote dir).
docker compose up -d

# 2. Configure .env for network mode.
# Set SCOUT_PUBLIC_HOST to your LAN IP if other agents will call you from
# other machines. (`ip -4 addr` on Linux, `ipconfig getifaddr en0` on macOS.)

# 3. Start Scout in serve mode (REPL + A2A server in the same process).
.venv/bin/scout serve -v
```

Server-only mode (no REPL):

```bash
.venv/bin/scout serve --no-repl -v
```

## Configuration

All config is environment variables (loaded from `.env` automatically via
python-dotenv). Required keys:

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | LLM provider | (required) |
| `BRAVE_API_KEY` | Brave Search API | (required) |

Network-mode keys (only used by `scout serve`):

| Variable | Purpose | Default |
|---|---|---|
| `SCOUT_AGENT_NAME` | Identity in the directory | `Scout` |
| `DIRECTORY_ENDPOINT` | AGNTCY directory gRPC `host:port` | `127.0.0.1:8889` |
| `SCOUT_HTTP_HOST` | Bind address for the JSON-RPC server | `0.0.0.0` |
| `SCOUT_HTTP_PORT` | Port for the JSON-RPC server | `9104` |
| `SCOUT_PUBLIC_HOST` | Host advertised in the AgentCard URL | `127.0.0.1` |
| `SCOUT_PEER_SKILLS` | Optional CSV of OASF skill IDs / agent names to additionally search for | (empty) |

### `SCOUT_PUBLIC_HOST` matters

The URL Scout advertises in its AgentCard is built from `SCOUT_PUBLIC_HOST` +
`SCOUT_HTTP_PORT`. Other agents read this URL from the directory and POST to
it. If you leave `SCOUT_PUBLIC_HOST=127.0.0.1`, peers on other machines will
read "127.0.0.1" as their *own* localhost and never reach you. Set it to
your LAN IP (or a hostname that resolves correctly from peer machines).

## OASF skills

Scout declares six OASF taxonomy skills so directory indexing emits proper
`/skills/<path>/<leaf>` labels and skill-based discovery can find Scout. See
[scout/network/card.py](scout/network/card.py) for the canonical list:

- `natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search`
- `natural_language_processing/information_retrieval_synthesis/fact_extraction`
- `natural_language_processing/information_retrieval_synthesis/knowledge_synthesis`
- `natural_language_processing/natural_language_generation/summarization`
- `natural_language_processing/analytical_reasoning/inference_deduction`
- `retrieval_augmented_generation/retrieval_of_information/document_retrieval`

The SDK's `agent_card_to_oasf` defaults all skills to a placeholder ID 101 —
Scout overrides this in [`build_scout_oasf_record`](scout/network/card.py) to
publish proper IDs.

## Project structure

```
scout/
├── agent.py            ── ReAct loop + ScoutSession (multi-turn)
├── budget.py           ── Per-turn iteration / tool-call budget
├── config.py           ── Settings dataclass, env loading
├── prompts.py          ── System prompt + adaptive reply guidance
├── cli.py              ── argparse + `scout` / `scout serve` flows
├── capabilities/
│   ├── base.py         ── Capability ABC, CapabilityResult, Registry
│   ├── brave.py        ── Brave web + news search adapters
│   └── a2a_peer.py     ── Wrap a remote A2A agent as a Capability
└── network/
    ├── card.py         ── Build AgentCard + OASF record (with proper skill IDs)
    ├── server.py       ── A2A HTTP server bootstrap (AGNTCY app-sdk)
    ├── executor.py     ── ScoutAgentExecutor (a2a-sdk AgentExecutor impl)
    ├── client_factory.py  ── Outbound A2A client config
    └── directory.py    ── publish_self / unpublish_cid / discover_peers / cleanup
scripts/
├── smoke.sh            ── Phase-1 (no-network) end-to-end smoke
├── network_smoke.sh    ── Phase-2 (serve mode) smoke
├── call_scout.py       ── Call Scout via the directory + A2A (probe / demo)
├── list_dir.py         ── Browse all A2A agents in a directory
└── routing_list.py     ── Show routing-layer records with their /skills/* labels
docker-compose.yml      ── Optional: local AGNTCY directory daemon
infra/                  ── (bind-mount target for dir-data, gitignored)
```

## Verification

### One-shot mode (no network)

```bash
.venv/bin/bash scripts/smoke.sh
```

Asserts that the brief contains all four sections (Situation Summary / Trend
Observations / Sources / Confidence & Gaps) and at least one URL citation.

### Networked mode

Two terminals:

```bash
# Terminal A: start serve
.venv/bin/scout serve -v
```

```bash
# Terminal B: verify
ss -tlnp | grep 9104                                              # is server listening?
curl -sS http://localhost:9104/.well-known/agent.json | head -3   # local probe
.venv/bin/python scripts/list_dir.py Scout                        # dir record present?
.venv/bin/python scripts/routing_list.py                          # routing labels emitted?
.venv/bin/python scripts/call_scout.py "test mission"             # round-trip via A2A
```

`scripts/routing_list.py` shells out to `dirctl` in a docker container
(`ghcr.io/agntcy/dir-ctl`) — the Python SDK's `Client.list()` returns 0 records
against the routing layer (suspected upstream bug); `dirctl` works.

## Caveats and known issues

- **Coding Agent's URL bug** (lab observation): some peers in the test network
  declared `url=http://127.0.0.1:9106/`, which is unreachable from other hosts.
  If a peer's URL is `127.0.0.1`, `localhost`, or `0.0.0.0`, calls from
  off-host will fail. Not Scout's bug — but Scout will surface peer-call
  failures as `is_error` tool results and the LLM will fall back to other
  capabilities.
- **`agent_card_to_oasf` placeholder skill** (SDK behavior): the AGNTCY
  conversion helper hardcodes `skills=[{"id": 101}]`. Scout works around this
  by constructing its own OASF record via `build_scout_oasf_record`.
- **`Client.list()` returns 0** (suspected SDK bug): the Python directory
  SDK's `list()` against `agntcy-dir-ctl` returns no records even when records
  exist. `dirctl routing list` (CLI) works correctly. `routing_list.py` uses
  the CLI to bypass.
- **Stale-record cleanup is best-effort**: Scout cleans up its own records on
  graceful shutdown, and runs a defensive cleanup on startup. A hard crash
  (kill -9, OOM) skips cleanup; the next startup will repair.
- **Scope**: Scout refuses to do adversary attribution / target recommendations
  / operational planning even when asked. That's by design (see system prompt
  in [scout/prompts.py](scout/prompts.py)).

## Dependencies

```toml
openai >= 1.50.0          # LLM client
httpx >= 0.27.0           # Brave Search HTTP client
python-dotenv >= 1.0.0    # .env loading
agntcy-app-sdk >= 0.5.0   # AGNTCY directory + A2A app SDK
a2a-sdk >= 0.3.20         # A2A protocol
```

Default model is `gpt-5-mini`. Override with `--model <id>` or by changing the
default in [scout/config.py](scout/config.py).
