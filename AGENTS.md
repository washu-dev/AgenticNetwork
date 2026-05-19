# AGENTS.md

## Project direction

This repository has moved from the old image-classification prototype to the AGNTCY-based agent network.

The new target system is **not** an image classifier.

The new target system is an infrastructure demo where a PI agent receives tasks from Telegram, WhatsApp, or an HTTP API, discovers specialized organization-owned agents through AGNTCY Directory / ADS, and delegates tasks through A2A-compatible communication.

## Required planning document

Before making major changes, read:

```text
docs/V2_AGENT_NETWORK_MIGRATION.md
```

Treat that file as the authoritative implementation specification for the v2 migration.

## Working rules

- Prefer creating a new `v2_agent_network/` implementation area.
- Do not implement the real specialized agents' intelligence.
- Implement infrastructure, contracts, mock agents, discovery, A2A client/server abstractions, state, policy, messaging ingress, tests, and docs.
- Keep AGNTCY Directory / ADS and A2A conceptually separate:
  - ADS / Directory = publish and discover agent metadata.
  - A2A = agent-to-agent task, message, status, and artifact communication.
  - HTTP / SLIM / NATS = transport.
- Do not call arbitrary custom JSON messages “A2A” unless they preserve A2A task/message/artifact semantics.
- Use mock agents only to prove the infrastructure.
- Make adding a new agent runtime-discoverable through an Agent Card / OASF record, not a planner code edit.
- The PI agent should not hardcode service URLs for specialized agents. It should discover candidates through the Directory client.
- Large files, images, logs, and generated outputs should be passed as artifact references/URIs where possible, not repeatedly inlined through messages.

## Before coding

First inspect the repository and produce a migration plan that answers:

1. What legacy assumptions remain from the old image-classification prototype?
2. What should move under `v2_agent_network/`, `v3_main_agent/`, or root-level agent folders?
3. Whether there is already an AGNTCY Directory client.
4. Whether there is already an A2A client/server abstraction.
5. Whether current NATS usage is custom JSON or A2A-compatible.
6. The smallest file set needed for `v2_agent_network/`.
7. Risks, missing dependencies, and assumptions.

After the migration plan, proceed with implementation unless there is a blocking ambiguity.

## Validation

After implementation, run available tests and report:

- changed files
- tests run
- tests failed or skipped
- remaining limitations
- how to run the demo
