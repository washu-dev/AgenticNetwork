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
| [ONBOARDING.md](ONBOARDING.md) | **start here as a contributor** — how changes actually reach the running system, the right way to add an agent or a credential, and the common pitfalls (repo ≠ running, card file ≠ published, env-var vs per-user creds). |
| [ARCHITECTURE.md](ARCHITECTURE.md) | understand how the pieces fit — the three contracts (Agent Card / ADS / A2A), the end-to-end request lifecycle, and the load-bearing "stored vs. routed" ADS subtlety. |
| [AUTH.md](AUTH.md) | understand identity (Shibboleth), admin (`ADMIN_EMAILS`), per-user credentials, the two credential-sourcing models, and multi-user isolation. |
| [DECISIONS.md](DECISIONS.md) | know *why* something is the way it is — the durable architectural decisions and their rationale (ADR log). |
| [MULTI_CONTRIBUTOR.md](MULTI_CONTRIBUTOR.md) | understand how the shared ENGR instance was de-personalized (group image registry, prod vs isolated test instances, deploy lock) and what is still open (GitHub App, CI build/deploy). |
| [PLANNER.md](PLANNER.md) | work on the orchestrator — the tool loop, ADS/A2A dispatch, policy gating, context/session persistence. |
| [REGISTRATION_SERVICE.md](REGISTRATION_SERVICE.md) | work on agent registration, the custom-agent upload lanes, or the approval flow. |
| [AGENT_DIRECTORY.md](AGENT_DIRECTORY.md) | operate or debug ADS — publishing, the routing index, repair tooling. |
| [BUILDING_AGENTS.md](BUILDING_AGENTS.md) | build a new agent — the skeleton, the two executor lanes, the answers→agent generator, and cards. |
| [DEVSTACK.md](DEVSTACK.md) | run the whole system on a laptop with one command (`scripts/stack up`), switch the LLM, add agents. |
| [TESTING_CHANGES.md](TESTING_CHANGES.md) | test a change on the ENGR cluster on your own throwaway instance instead of the shared prod deployment. |
| [OPEN_SOURCE_RELEASE.md](OPEN_SOURCE_RELEASE.md) | work on the open-source release — what ships, the `agent_skeleton` subtree plan, sprint priorities, and the full-chain test and one-command start goals (in progress, 2026-10). |

## Related root docs
- [`CLAUDE.md`](../CLAUDE.md) — system guide + operating manual (agent- and human-facing).
- [`AGENTS.md`](../AGENTS.md) — project direction + working rules.
- [`SECURITY.md`](../SECURITY.md) — security posture + active hazards.
- [`../deployment/`](../deployment/) — deployment **machinery** (Dockerfiles, entrypoints, launch scripts).
- Each component (`planner_agent/`, `registration_service/`, `agent_directory_service/`,
  `agent_skeleton/`) has its own `README.md` with authoritative install/run steps.

## Deployment

Deployment guides (the machinery lives in [`../deployment/`](../deployment/)):

| Doc | Read it when you need to… |
|---|---|
| [deployment/README.md](deployment/README.md) | **start here** — the two targets (cluster / GCP) and how to read them side-by-side. |
| [deployment/engr/README.md](deployment/engr/README.md) | run/operate on the **ENGR cluster** (Slurm + Pyxis/enroot) — the live public deployment. |
| [deployment/gcp/README.md](deployment/gcp/README.md) | run/operate on **GCP** (Cloud Run + Cloud SQL) — architecture, how to access, dev workflow, cluster differences. Built to a working milestone in 2026-08 and on hold since; cloud is a critical leg going forward (2026-10-13). |
| [deployment/gcp/MIGRATION.md](deployment/gcp/MIGRATION.md) · [DEPLOYED_COMPONENTS](deployment/gcp/DEPLOYED_COMPONENTS.md) | GCP design/status, and the live inventory. |
| [deployment/DOWNSTREAM_CONSIDERATIONS.md](deployment/DOWNSTREAM_CONSIDERATIONS.md) · [AGENT_INTEGRATION_SPEC.md](deployment/AGENT_INTEGRATION_SPEC.md) | target-agnostic: forward-looking decisions, and the agent-integration contract. |
