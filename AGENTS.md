# AGENTS.md

## Project Direction

The system is split across two repos:

- **This repo (`AgenticNetwork`)** owns the infrastructure: planner, ADS operator
  tooling, registration service, and the `agent_skeleton` template.
- **[`agent-directory`](https://github.com/washu-dev/agent-directory)** owns the
  specialist agents: email, coding, intelligence, geoSpatial, disaster_response, scout.

Within this repo:
- `agent_directory_service/` owns ADS operation, publication, route inspection,
  diagnostics, and repair tooling.
- `planner_agent/` owns the product main-agent runtime.
- `agent_skeleton/` is the canonical template for building new agents.

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
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 agent_directory_service/.venv/bin/python -m pytest agent_directory_service/tests -q
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.print_versions
```
