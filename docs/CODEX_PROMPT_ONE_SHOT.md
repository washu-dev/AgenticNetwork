# Codex Prompt: One-Shot Migration

Use this only if you want Codex to audit and implement in one pass.

```text
Read AGENTS.md and docs/V2_AGENT_NETWORK_MIGRATION.md.

We are migrating this repo from the old v1 image-classification demo to the new v2 AGNTCY-based PI-agent network.

First inspect the repository and produce the required migration plan from AGENTS.md.

Then implement the v2 system under v2_agent_network/ according to docs/V2_AGENT_NETWORK_MIGRATION.md.

Do not delete the old v1 code.

Start with the smallest working infrastructure:

- PI agent
- DirectoryClient abstraction
- Agent Card/OASF models
- A2A client/server abstraction
- mock Email, Calendar, Weather, Photo/Geospatial, Intelligence, and Coding agents
- HTTP API fallback for user messages
- routing, policy, state, demo script, tests, and README

Do not implement the real specialized agents' intelligence.

After implementation:
- run relevant tests
- summarize changed files
- summarize test results
- list remaining limitations
- explain how to run the demo
```
