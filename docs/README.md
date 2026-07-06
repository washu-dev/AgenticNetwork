# AgenticNetwork — Developer Reference

The curated, source-of-truth reference for developers. Start here.

For the fastest orientation, read the repo-root [`CLAUDE.md`](../CLAUDE.md) (what the
system is, how to run it, the component/port map). These docs go **deeper** than
CLAUDE.md's summaries — they don't repeat run instructions, they explain internals,
decisions, and rationale.

> **Scope:** only durable reference lives here. In-progress plans, handoffs, and working
> notes live in `docs/dev/` (git-ignored, local-only) and are **not** source of truth.

## Map

| Doc | Read it when you need to… |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | understand how the pieces fit — the three contracts (Agent Card / ADS / A2A), the end-to-end request lifecycle, and the load-bearing "stored vs. routed" ADS subtlety. |
| [AUTH.md](AUTH.md) | understand identity (Shibboleth), admin (`ADMIN_EMAILS`), per-user credentials, the two credential-sourcing models, and multi-user isolation. |
| [DECISIONS.md](DECISIONS.md) | know *why* something is the way it is — the durable architectural decisions and their rationale (ADR log). |
| [PLANNER.md](PLANNER.md) | work on the orchestrator — the tool loop, ADS/A2A dispatch, policy gating, context/session persistence. |
| [REGISTRATION_SERVICE.md](REGISTRATION_SERVICE.md) | work on agent registration, the custom-agent upload lanes, or the approval flow. |
| [AGENT_DIRECTORY.md](AGENT_DIRECTORY.md) | operate or debug ADS — publishing, the routing index, repair tooling. |
| [BUILDING_AGENTS.md](BUILDING_AGENTS.md) | build a new agent — the skeleton, the two executor lanes, the answers→agent generator, and cards. |

## Related root docs
- [`CLAUDE.md`](../CLAUDE.md) — system guide + operating manual (agent- and human-facing).
- [`AGENTS.md`](../AGENTS.md) — project direction + working rules.
- [`SECURITY.md`](../SECURITY.md) — security posture + active hazards.
- [`deployment/README.md`](../deployment/README.md) — ENGR cluster deployment runbook.
- Each component (`planner_agent/`, `registration_service/`, `agent_directory_service/`,
  `agent_skeleton/`) has its own `README.md` with authoritative install/run steps.
