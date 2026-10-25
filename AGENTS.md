# AGENTS.md

**Current focus (2026-10-13): open-source release prep.** See
[`docs/OPEN_SOURCE_RELEASE.md`](docs/OPEN_SOURCE_RELEASE.md) for scope, the
`agent_skeleton` subtree plan, sprint priorities, and the full-chain test and
one-command start goals.

## Project Direction

The system is split across two repos:

- **This repo (`AgenticNetwork`)** owns the infrastructure: the chat app (with its
  planner), the registration service (which also builds and runs registered agents),
  the shared `agentnet` library with the ADS operator tooling, and the `agent_skeleton`
  agent runtime.
- **[`agent-directory`](https://github.com/washu-dev/agent-directory)** owns the
  specialist agents: email, coding, intelligence, geoSpatial, disaster_response, scout.

Within this repo:
- `agentnet/` is the one shared directory (ADS) and A2A library that the chat app
  and the registration service import. It also owns ADS operation, publication,
  route inspection, diagnostics, and repair tooling (`agentnet/scripts/`, run
  through `scripts/ads`).
- `chat_app/` is the web service people sign in to and chat in. The planner
  (`chat_app/planner/`) is the LLM loop inside it that chooses agents and calls them.
- `agent_skeleton/` is the agent runtime that every hosted agent imports, plus
  a small demo agent and the wrapper generator. Build new agents as
  [`docs/BUILDING_AGENTS.md`](docs/BUILDING_AGENTS.md) describes, not by copying
  or editing it; its import name is a public interface.

Do not reintroduce the old prototype, milestone demo agents, or PI runtime under
`agentnet/`. ADS and A2A should stay conceptually separate:

- ADS / Directory publishes and discovers agent metadata.
- A2A handles direct agent-to-agent task communication after discovery.
- Agent Cards are the contract between ADS metadata and each agent endpoint.

## Working Rules

- Prefer small, component-local changes.
- Keep generated files, logs, local env files, bytecode, and virtualenvs out of
  git.
- Do not hardcode specialized-agent endpoints into ADS logic. ADS should read
  Agent Cards and records.
- Preserve real ADS behavior when cleaning tests or examples: `publish_agents`,
  `show_ads_routes`, `ads_diagnostics`, `watch_ads_routing`, and
  `force_route_active_agents` are core operator tools.
- When validating ADS changes, run the ADS package tests and at least one import
  smoke test from the repository root.

## Validation

For ADS changes:

```bash
scripts/stack test agentnet
registration_service/.venv/bin/python -m agentnet.scripts.print_versions
```

agentnet runs in both service venvs (registration on `a2a-sdk` 0.3.2, the chat app
on 0.3.26), so `scripts/stack test agentnet` runs its tests with both; `scripts/ads`
uses the registration venv (`ADS_PYTHON` overrides it). All suites: `scripts/stack test`
(`CLAUDE.md` §10); CI runs the same command (`.github/workflows/tests.yml`).
