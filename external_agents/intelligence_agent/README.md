# Intelligence Agent

Autonomous Intelligence Agent for the AgenticNetwork. The agent is itself an
LLM that drives a tool-calling ReAct loop: it pulls current mission status
from a peer recon agent (forward spider-robot scouts), optionally pulls
external context via Brave Search or an OSINT scout agent, and synthesizes
**Situational Awareness / Threat Assessment / Course of Action / Information
Gaps** for a decision-maker.

It is the "operational planning + threat assessment" half of the OSINT/intel
split that the Scout agent's README explicitly carves out — Scout does
environment-centric open-source research; this agent does adversary
tracking, target categorization, and COA synthesis on top of recon and OSINT
inputs.

## Architecture (mirrors the coding agent)

```
PI / user (A2A message/send)
    -> IntelligenceAgentExecutor (intelligence_agent.py)
        -> run_agent_loop (agent_loop.py)
            tools:
              discover_agents(skill_query)        -> AGNTCY directory
              query_peer_agent(skill_query, ...)  -> A2A message/send to peer
              web_search(query, count=5)          -> Brave Search
        -> structured_output (peer_calls, web_search_calls,
                              agent.final_message, tool_calls trace)
```

The same dirctl + ADS integration the coding agent uses — on `serve-a2a` with
`--directory-server`, the intelligence agent self-publishes its OASF record
on startup and unpublishes on exit. Peer discovery walks
`dirctl routing list` and pulls each A2A record, filtered by
`annotations.agentic_network.custom_skill_tags`.

## Tools

| Tool | Purpose |
|---|---|
| `discover_agents(skill_query)` | Search the AGNTCY Directory for agents claiming a skill (e.g. `recon/spider_robots`, `research/open-source-intelligence`). Returns endpoint + name + skills. |
| `query_peer_agent(skill_query, prompt, agent_id=None)` | Send a natural-language A2A `message/send` request to a discovered peer. Returns the peer's text reply + `structured_output`. Discovery is cached per request — repeated calls with the same `skill_query` reuse the first lookup. |
| `web_search(query, count=5)` | Brave Search (`https://api.search.brave.com/res/v1/web/search`). Requires `BRAVE_API_KEY`. Returns `{title, url, snippet, age}` per hit. |

## The recon (spider-robots) peer agent

This agent talks to the **Multi-Spider Command Center Planner** (an
external A2A agent that owns the spider-robot fleet's mission graph,
conversation memory, and per-task summarisation; default endpoint
`http://10.229.117.188:9100/`). It advertises custom skill tags
namespaced `multi_spider/*`. The intelligence agent picks the right one
per question:

| User question | skill_query the LLM should pick |
|---|---|
| "What did the fleet do recently?" / recap | `multi_spider/summarize_recent_tasks` |
| "What's there now in zone X?" / sitrep | `multi_spider/observe` |
| "Is there a tank / person / X?" | `multi_spider/find_target` |
| "Alert me if X appears." | `multi_spider/watch_alarm` |
| "Which robots are online / available?" | `multi_spider/robot_status` |
| "How far is Y?" | `multi_spider/estimate_distance` |
| "Restate that as SITREP / civilian prose." | `multi_spider/reformat_last_summary` |

The intelligence agent does **not** invoke `multi_spider/move_robots` —
this agent is an analyst, not an operator.

The peer accepts an A2A `message/send` whose user message contains a
natural-language question; the system prompt instructs the LLM to mention
the specific `multi_spider/*` skill tag inside the prompt as well so the
peer routes correctly.

Other peers the LLM may discover when present:

- `research/open-source-intelligence` — OSINT brief from a Scout-style
  agent.
- `intelligence/situation_summary` — sibling intelligence agent.

The test suite stands up a stub recon peer to drive the full agent loop,
and `--fake-ads` (in `run-once`) lets you wire any mock peer through the
in-memory directory if the real planner isn't running.

## Skills published in the Agent Card

The Agent Card at
`v2_agent_network/configs/agent_cards/intelligence_agent.card.json` lists
five skills (situation_summary, risk_assessment, course_of_action,
entity_tracking, report_generation). These are **discoverability hints** —
peers find this agent by `intelligence/*` skill tags. They are **not**
routing keys: there is no `metadata.skill` switch in the executor; the
single LLM loop handles all five.

## Quick start

### Prerequisites

- Python 3.12+
- `OPENAI_API_KEY` (for the default `openai/gpt-5-mini` planner)
- `BRAVE_API_KEY` for `web_search` (optional — without it, the tool returns
  `not_configured` and the LLM relies on peers only)
- For networked mode: a reachable AGNTCY directory daemon and the `dirctl`
  CLI binary on PATH (or `--dirctl-path`)

### Install

```bash
cd external_agents/intelligence_agent
conda env create -f environment.yml
conda activate intelligence-agent-a2a
```

### Configure

```bash
export INTEL_AGENT_MODEL=openai/gpt-5-mini       # litellm model id
export OPENAI_API_KEY=sk-...
export BRAVE_API_KEY=BSA...                      # optional
export INTEL_AGENT_DIRECTORY_URL=10.229.117.37:8888   # ADS server
```

### Run (serve as A2A node)

```bash
python intelligence_agent.py serve-a2a \
    --host 0.0.0.0 --port 9105 \
    --directory-server $INTEL_AGENT_DIRECTORY_URL
```

The agent self-publishes to ADS on startup, listens for A2A `message/send`
requests, and unpublishes on shutdown. `--no-publish` keeps discovery on
but skips self-registration.

### Run (single CLI request, no A2A round-trip)

```bash
python intelligence_agent.py run-once \
    --prompt "Brief grid ALPHA-3 — current contacts, civilian presence, recommended COA." \
    --directory-server $INTEL_AGENT_DIRECTORY_URL
```

### Test client

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
| `BRAVE_API_KEY` | enables `web_search` | (unset, tool disabled) |
| `INTEL_AGENT_DIRECTORY_URL` | ADS server addr for `dirctl` | (unset) |
| `ADS_URL` | fallback for directory addr | (unset) |
| `INTEL_AGENT_A2A_HOST` | bind interface for serve-a2a | `0.0.0.0` |
| `INTEL_AGENT_A2A_PORT` | bind port for serve-a2a | `9105` |
| `INTEL_AGENT_LOG_LEVEL` | debug / info / warning / ... | `info` |
| `AGENT_NETWORK_FAKE_ADS` | use the in-memory fake directory | `false` |

## Running tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTHONPATH=external_agents/intelligence_agent \
  AgenticNetwork/v1_classification/.venv/bin/python -m pytest \
  AgenticNetwork/external_agents/intelligence_agent/tests -q
```

The test suite stubs the LLM completion, the directory, the peer A2A
transport, and the Brave HTTP call — no external services are required.

## Scope and constraints

- This agent is an **analyst**, not an operator. It recommends actions; it
  does not direct kinetic strikes. The system prompt enforces this.
- Civilians are categorized distinctly from unknown/hostile entities.
  COAs that risk civilian harm are flagged in the brief.
- When the recon peer is unreachable, the brief is expected to surface
  "INFORMATION GAPS: recon channel unavailable" rather than fabricate
  mission state. The test suite asserts this behavior.

## Known gaps / future work

- **Recon peer not yet implemented** (see "Pending" above). The agent loop
  works end-to-end against the test stub today; once the real recon agent
  lands, no code change is required here.
- **No pause/resume yet.** The pause/resume machinery from the coding
  agent (`PendingUserConfirmation`, state store) is preserved in
  `agent_loop.py` for symmetry but no intelligence tool currently raises
  it. If a future tool wants human-in-the-loop confirmation (e.g.
  "escalate this finding to a human analyst"), wire it in like the coding
  agent's `send_email_via_directory` did.
- **OASF skill IDs in `_ads_record.py`** are taken from the scout agent's
  canonical mapping. If real ADS rejects them, update both files in
  lockstep.
