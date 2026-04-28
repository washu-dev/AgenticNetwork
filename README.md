
## Files

- `AGENTS.md`
  - Put this at the repository root.
  - Codex will use it as persistent project guidance.

- `docs/V2_AGENT_NETWORK_MIGRATION.md`
  - Put this under the repository's `docs/` directory.
  - This is the detailed implementation specification.

- `docs/CODEX_PROMPT_AUDIT_FIRST.md`
  - Optional prompt for a first Codex pass that only audits and plans.

- `docs/CODEX_PROMPT_IMPLEMENT_AFTER_AUDIT.md`
  - Optional prompt for the second Codex pass that implements the approved plan.

- `docs/CODEX_PROMPT_ONE_SHOT.md`
  - Optional prompt if you want Codex to audit and implement in one pass.

## Core thesis

The v2 system is not an image classifier. It is an AGNTCY-based multi-agent infrastructure demo:

```text
PI user on Telegram / WhatsApp / HTTP API
  -> Messaging ingress
  -> PI Agent
  -> AGNTCY Directory / ADS discovery
  -> A2A task delegation
  -> Specialized agents
  -> task status / artifacts
  -> PI Agent response
```
