# Intelligence Agent

Standalone, self-contained A2A service for **operational planning + threat
assessment**. The agent is itself an LLM driving a tool-calling ReAct loop:
it pulls current mission status from a peer recon agent (forward
spider-robot scouts), optionally pulls external context via Brave Search or
an OSINT scout agent, and synthesizes **Situational Awareness / Threat
Assessment / Course of Action / Information Gaps** for a decision-maker.

It is the "analyst" half of the OSINT/intel split — the Scout agent does
environment-centric open-source research; this agent does adversary
tracking, target categorization, and COA synthesis on top of recon and
OSINT inputs.

This directory has no runtime dependency on any sibling package. The shared
A2A SDK boundary, directory client, and Pydantic models it relies on are
vendored under [`_vendor_v2/`](./_vendor_v2).

## At a glance

```
A2A message/send (user prompt)
    │
    ▼
IntelligenceAgentExecutor    (intelligence_agent.py)
    │
    ▼
LLM planner loop             (agent_loop.py)
    tools:
      discover_agents(skill_query)
      query_peer_agent(skill_query, prompt, agent_id=None)
      web_search(query, count=5)
      send_group_email_via_directory(subject, body, importance="normal")
    │
    ▼
structured_output {
  peer_calls[], web_search_calls[], delivery_results[],
  agent { final_message, exit_reason, n_steps, elapsed_s, tool_calls[] }
}
```

## Tools

| Tool | Purpose |
|---|---|
| `discover_agents(skill_query)` | Search the AGNTCY Directory for agents claiming a skill (e.g. `multi_spider/observe`, `research/open-source-intelligence`). Returns endpoint + name + skills. Cached per request. |
| `query_peer_agent(skill_query, prompt, agent_id=None)` | Send a natural-language A2A `message/send` to a discovered peer. Returns the peer's text reply + `structured_output`. Discovery is cached — repeated calls with the same `skill_query` reuse the first lookup. `agent_id` is optional and pins the call to a specific peer in the match set. |
| `web_search(query, count=5)` | Brave Search (`https://api.search.brave.com/res/v1/web/search`). Requires `BRAVE_API_KEY`; without it the tool returns `not_configured` and the LLM falls back to peer-only context. |
| `send_group_email_via_directory(subject, body, importance="normal")` | Discover an `email/group_send`-capable agent and broadcast `(subject, body)` to its configured notice list. The email agent owns the recipient list; this agent only ships the message + an optional `importance` flag (`normal` or `high`). |

The system prompt rules forbid `multi_spider/move_robots` — this agent is
an analyst, not an operator.

## Recon peer (`multi_spider/*`)

In production the recon peer is the **Multi-Spider Command Center Planner**
(external A2A agent owning the spider-robot fleet's mission graph,
conversation memory, and per-task summarisation). Default endpoint configurable via `--directory-server` or `INTEL_AGENT_DIRECTORY_URL`. It advertises custom skill tags namespaced
`multi_spider/*`:

| User question | `skill_query` the LLM should pick |
|---|---|
| "What did the fleet do recently?" / recap | `multi_spider/summarize_recent_tasks` |
| "What's there now in zone X?" / sitrep | `multi_spider/observe` |
| "Is there a tank / person / X?" | `multi_spider/find_target` |
| "Alert me if X appears." | `multi_spider/watch_alarm` |
| "Which robots are online / available?" | `multi_spider/robot_status` |
| "How far is Y?" | `multi_spider/estimate_distance` |
| "Restate that as SITREP / civilian prose." | `multi_spider/reformat_last_summary` |

The peer accepts an A2A `message/send` whose user message contains a
natural-language question; the system prompt instructs this agent's LLM to
mention the specific `multi_spider/*` skill tag inside the prompt as well
so the peer routes correctly.

Other peers the LLM may discover when present:

- `research/open-source-intelligence` — OSINT brief from a Scout-style
  agent.
- `intelligence/situation_summary` — sibling intelligence agent.

When the recon peer is unreachable the brief is expected to surface
`INFORMATION GAPS: recon channel unavailable` rather than fabricate
mission state.

## Skills published in the Agent Card

`intelligence_agent.card.json` (next to this README) lists five skills:
`intelligence/situation_summary`, `risk_assessment`, `course_of_action`,
`entity_tracking`, `report_generation`. These are **discoverability hints**
— peers find this agent by `intelligence/*` skill tags. They are **not**
routing keys: there is no `metadata.skill` switch in the executor; the
single LLM loop handles all five.

## ADS publish / unpublish (`dirctl`)

When `serve-a2a` is given `--directory-server <addr>` (or
`INTEL_AGENT_DIRECTORY_URL` / `ADS_URL`), the intelligence agent
self-publishes its OASF record on startup and unpublishes on clean exit —
the same dirctl-based flow the coding agent uses. Peer discovery walks
`dirctl routing list --output json`, pulls each A2A record, and filters by
`annotations.agentic_network.custom_skill_tags`. Use `--no-publish` to
keep discovery on but skip self-registration.

## Setup

```bash
cd intelligence_agent
conda env create -f environment.yml
conda activate intelligence-agent-a2a
```

Configure:

```bash
export INTEL_AGENT_MODEL=openai/gpt-5-mini             # litellm model id
export OPENAI_API_KEY=sk-...
export BRAVE_API_KEY=BSA...                            # optional; enables web_search
export INTEL_AGENT_DIRECTORY_URL=localhost:8888         # ADS server
```

## Run

```bash
# Serve as A2A node
python intelligence_agent.py serve-a2a \
    --host 0.0.0.0 --port 9105 \
    --directory-server $INTEL_AGENT_DIRECTORY_URL

# Single CLI request (no A2A round-trip)
python intelligence_agent.py run-once \
    --prompt "Brief grid ALPHA-3 — current contacts, civilian presence, recommended COA." \
    --directory-server $INTEL_AGENT_DIRECTORY_URL

# Or with the in-memory fake directory:
python intelligence_agent.py run-once \
    --prompt "..." --fake-ads

# List the skills the executor advertises
python intelligence_agent.py tools
```

`serve-a2a` flags:

| Flag | Purpose |
|---|---|
| `--card PATH` | Override the in-tree `intelligence_agent.card.json`. |
| `--directory-server ADDR` | ADS server for `dirctl` (publish + discovery). |
| `--no-publish` | Use `--directory-server` for discovery only. |
| `--dirctl-path PATH` | Path to the `dirctl` binary. Defaults to `PATH` lookup. |
| `--allow-missing-model` | Skip the upfront `INTEL_AGENT_MODEL` check (useful for tests). |
| `--log-level debug` | Per-step tool-call traces. |

## Test client

```bash
python send_request.py \
    --endpoint http://127.0.0.1:9105/ \
    --prompt "What is the current threat picture in sector 7?"
```

## Configuration reference

| Variable | Purpose | Default |
|---|---|---|
| `INTEL_AGENT_MODEL` | litellm model id for the planner LLM | `openai/gpt-5-mini` |
| `INTEL_AGENT_PLANNER_MODEL` | overrides `INTEL_AGENT_MODEL` if set | (unset) |
| `OPENAI_API_KEY` | required for the default model | — |
| `BRAVE_API_KEY` | enables `web_search` | (unset, tool returns `not_configured`) |
| `INTEL_AGENT_DIRECTORY_URL` | ADS server addr for `dirctl` | (unset) |
| `ADS_URL` | fallback for directory addr | (unset) |
| `INTEL_AGENT_A2A_HOST` | bind interface for `serve-a2a` | `0.0.0.0` |
| `INTEL_AGENT_A2A_PORT` | bind port for `serve-a2a` | `9105` |
| `INTEL_AGENT_LOG_LEVEL` | `debug` / `info` / `warning` / … | `info` |
| `AGENT_NETWORK_FAKE_ADS` | use the in-memory fake directory | `false` |

## Running the test suite

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
PYTHONPATH=intelligence_agent \
pytest intelligence_agent/tests -q
```

The test suite stubs the LLM completion, the directory, the peer A2A
transport, and the Brave HTTP call — no external services are required.

## Scope and constraints

- This agent is an **analyst**, not an operator. It recommends actions; it
  does not direct kinetic strikes. The system prompt enforces this.
- Civilians are categorized distinctly from unknown/hostile entities.
  COAs that risk civilian harm are flagged in the brief.
- When the recon peer is unreachable, the brief is expected to surface
  `INFORMATION GAPS: recon channel unavailable` rather than fabricate
  mission state.

## Directory layout

```
intelligence_agent/
├── intelligence_agent.py        # CLI + A2A executor
├── agent_loop.py                # LLM planner loop (tool_calls until terminal)
├── _ads_record.py               # Agent Card -> OASF schema 1.0.0 (publish path)
├── _vendor_v2/                  # vendored shared libs (no external dependency)
│   ├── common/models.py
│   ├── directory/{client,backends,card_oasf_converter}.py
│   └── a2a/{client,transport,adapters}.py
├── intelligence_agent.card.json # Agent Card for ADS publication
├── send_request.py              # A2A test client
├── environment.yml              # conda env: intelligence-agent-a2a
└── tests/                       # pytest suite
```
