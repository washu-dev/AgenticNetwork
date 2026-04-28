# Codex Prompt: Audit First

Paste this into Codex after placing `AGENTS.md` and `docs/V2_AGENT_NETWORK_MIGRATION.md` in the repo.

```text
Read AGENTS.md and docs/V2_AGENT_NETWORK_MIGRATION.md.

Do not edit files yet.

Inspect the repository and produce a migration plan for implementing v2_agent_network/.

Your plan must answer:

1. What parts of v1 can be reused?
2. What parts should remain as legacy/v1?
3. Whether an AGNTCY Directory client already exists.
4. Whether an A2A client/server abstraction already exists.
5. Whether NATS is currently custom JSON or A2A-compatible.
6. The smallest file set needed for v2_agent_network/.
7. Risks, assumptions, and missing dependencies.

Stop after the plan.
```
