# Codex Prompt: Implement After Audit

Paste this into Codex after reviewing the audit/migration plan.

```text
Implement the approved migration plan.

Follow AGENTS.md and docs/V2_AGENT_NETWORK_MIGRATION.md.

Create the v2_agent_network/ infrastructure while preserving v1 code.

Prioritize:

1. shared models
2. Agent Cards and OASF conversion helpers
3. DirectoryClient with real/fake backend separation
4. A2A client/server abstraction
5. PI agent router and policy
6. messaging HTTP fallback
7. mock agents
8. state store
9. demo script
10. tests
11. README

Do not implement the real specialized agents' intelligence.

Use mock agents only to prove the infrastructure.

After implementation:
- run available tests
- summarize changed files
- summarize test results
- list limitations
- explain how to run the demo
```
