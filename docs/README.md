# AgenticNetwork documentation

A map of every doc, grouped by who it is for. New here? Read the repo-root
[`README.md`](../README.md) first (what the system is, the quickstart), then come back.

These docs go deeper than the root README and [`CLAUDE.md`](../CLAUDE.md) (the single-file
system guide): they explain internals, decisions and rationale rather than repeat run steps.
Each component (`planner_agent/`, `registration_service/`, `agent_directory_service/`,
`agent_skeleton/`, `ads_utils/`) has its own `README.md` with its install and run steps.

> **Scope:** only durable reference lives here. In-progress plans and working notes go in
> `docs/dev/` (ignored by git, local only) and are not a source of truth.

## Users

| Doc | Read it to… |
|---|---|
| [Root README: Using the WashU deployment](../README.md#using-the-washu-deployment) | sign in to the WashU instance, chat, connect your keys. |
| [DEVSTACK.md](DEVSTACK.md) | try the whole system on a laptop with one command (`scripts/stack up`), switch the LLM, add agents. |

## Agent builders

| Doc | Read it to… |
|---|---|
| [BUILDING_AGENTS.md](BUILDING_AGENTS.md) | build a new agent: the skeleton, the executor lanes, the generator, and cards. |
| [deployment/AGENT_INTEGRATION_SPEC.md](deployment/AGENT_INTEGRATION_SPEC.md) | know what the system needs from an agent, and which dependency needs each piece. |
| [REGISTRATION_SERVICE.md](REGISTRATION_SERVICE.md) | register an agent: the handler types, the custom-upload lanes, the approval flow. |
| [ONBOARDING.md](ONBOARDING.md) | see how changes reach the running system, the right way to add an agent or a credential, and the common pitfalls (repo ≠ running, card file ≠ published, env-var vs per-user credentials). |

## Contributors

| Doc | Read it to… |
|---|---|
| [CONTRIBUTING.md](../CONTRIBUTING.md) | set up the venvs, run the tests, see what CI checks, open a pull request. |
| [TESTING.md](TESTING.md) | find each test suite, what it covers and how to run it. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | understand how the pieces fit: the three contracts (Agent Card, directory, A2A), the request lifecycle, the "stored vs. routed" directory subtlety. |
| [PLANNER.md](PLANNER.md) | work on the orchestrator: the tool loop, directory and A2A dispatch, policy gating, sessions. |
| [AUTH.md](AUTH.md) | understand identity, admin (`ADMIN_EMAILS`), per-user credentials and their scoping, and multi-user isolation. |
| [DECISIONS.md](DECISIONS.md) | know *why* something is the way it is: the architectural decision log. |
| [OPEN_SOURCE_RELEASE.md](OPEN_SOURCE_RELEASE.md) | follow the open-source release: what ships, the `agent_skeleton` subtree plan, open security items, deferred simplifications (working document, 2026-10). |

## Operators and deployers

The guides are here; the machinery (Dockerfiles, entrypoints, launch scripts) is in the repo-root
[`deployment/`](../deployment/README.md).

| Doc | Read it to… |
|---|---|
| [deployment/README.md](deployment/README.md) | start here: the deployment targets and how their guides line up. |
| [deployment/COMPOSE.md](deployment/COMPOSE.md) | run the system with Docker Compose on one host or a cloud VM. |
| [deployment/gcp/DEPLOY_YOUR_OWN.md](deployment/gcp/DEPLOY_YOUR_OWN.md) | deploy your own instance on Google Cloud Run. |
| [deployment/engr/README.md](deployment/engr/README.md) | run or operate the WashU ENGR cluster deployment (Slurm + Pyxis/enroot), an HPC example and the live WashU instance. |
| [TESTING_CHANGES.md](TESTING_CHANGES.md) | test a change on the ENGR cluster in your own throwaway instance instead of the shared one. |
| [MULTI_CONTRIBUTOR.md](MULTI_CONTRIBUTOR.md) | see how the shared ENGR instance was de-personalized (group image registry, prod vs. test instances, deploy lock) and what is still open. |
| [deployment/gcp/README.md](deployment/gcp/README.md) | operate the WashU GCP deployment (Cloud Run + Cloud SQL): architecture, access, dev workflow, differences from the cluster. Built to a working milestone in 2026-08, on hold since. |
| [deployment/gcp/MIGRATION.md](deployment/gcp/MIGRATION.md) | follow the GCP migration: goal, decisions, phases, status, gotchas. |
| [deployment/gcp/DEPLOYED_COMPONENTS.md](deployment/gcp/DEPLOYED_COMPONENTS.md) | see what ran on GCP as of 2026-08-03 (re-verify), with control commands. |
| [deployment/gcp/DESIGN_CLOUDRUN_BACKEND.md](deployment/gcp/DESIGN_CLOUDRUN_BACKEND.md) | understand the design of the register → build → deploy → publish automation on Cloud Run. |
| [deployment/gcp/IMPLEMENTATION_CLOUDRUN_BACKEND.md](deployment/gcp/IMPLEMENTATION_CLOUDRUN_BACKEND.md) | follow the step-by-step build of that automation and its progress. |
| [deployment/DOWNSTREAM_CONSIDERATIONS.md](deployment/DOWNSTREAM_CONSIDERATIONS.md) | read the forward-looking architecture and scaling decisions (an append-only log). |
| [AGENT_DIRECTORY.md](AGENT_DIRECTORY.md) | operate or debug the directory (ADS): publishing, the routing index, diagnosis and repair (`agent_directory_service/run_ads.sh`). |

## Root files

- [`README.md`](../README.md): what the system is, quickstart, deployment options, layout.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md): setup, tests, CI, pull requests, conventions.
- [`SECURITY.md`](../SECURITY.md): how to report a vulnerability, the security model, known hazards.
- [`CLAUDE.md`](../CLAUDE.md): single-file system guide and operating manual (written for coding agents).
- [`AGENTS.md`](../AGENTS.md): project direction and working rules.
