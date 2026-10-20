# AGENTS.md

**Current focus (2026-10-13): open-source release prep.** See
[`docs/OPEN_SOURCE_RELEASE.md`](docs/OPEN_SOURCE_RELEASE.md) for scope, the
`agent_skeleton` subtree plan, sprint priorities, and the full-chain test and
one-command start goals.

## Project Direction

The system is split across two repos:

- **This repo (`AgenticNetwork`)** owns the infrastructure: planner, ADS operator
  tooling, registration service, and the `agent_skeleton` agent runtime.
- **[`agent-directory`](https://github.com/washu-dev/agent-directory)** owns the
  specialist agents: email, coding, intelligence, geoSpatial, disaster_response, scout.

Within this repo:
- `agent_directory_service/` owns ADS operation, publication, route inspection,
  diagnostics, and repair tooling.
- `planner_agent/` owns the product main-agent runtime.
- `agent_skeleton/` is the agent runtime that every hosted agent imports, plus
  a small demo agent and the wrapper generator. Build new agents as
  [`docs/BUILDING_AGENTS.md`](docs/BUILDING_AGENTS.md) describes, not by copying
  or editing it; its import name is a public interface.

Do not reintroduce the old prototype, milestone demo agents, or PI runtime under
`agent_directory_service/`. ADS and A2A should stay conceptually separate:

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
registration_service/.venv/bin/python -m pytest -q -p no:cacheprovider agent_directory_service/tests
registration_service/.venv/bin/python -m agent_directory_service.scripts.print_versions
```

The registration venv matches the ADS `a2a-sdk` 0.3.2 pin; `run_ads.sh` uses its own
`agent_directory_service/.venv`, built from `requirements.txt`. All suites and the dev stack:
`CLAUDE.md` §10; CI runs them (`.github/workflows/tests.yml`).
