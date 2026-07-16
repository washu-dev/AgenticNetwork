# AgenticNetwork → GCP Migration Proposal

**Amaan · 2026-07-13 · for Monday review.** Detailed design: `deployment/GCP_MIGRATION_PLAN.md`.

## Summary

Migrate the infrastructure from the WashU ENGR Slurm cluster to GCP for **scalability,
load testing, and cloud-native deployment**. The move is **additive** — Slurm stays a
working backup; nothing is deleted. Two sequenced tracks:

- **Track A — Control-plane migration (near-term, for the hackathon):** planner +
  registration on **Cloud Run**, database on **Cloud SQL**, LLM on **Vertex AI**, behind
  WashU login — then **load-test**.
- **Track B — On-demand agents (the $5k scalability demo):** spin up a right-sized agent
  **per query** and tear it down, instead of running all agents 24/7.

## Decisions

| Area | Decision | Why |
|---|---|---|
| **LLM** | **Vertex AI (managed Gemini)** | No GPU ops; credits apply via Vertex; drop-in OpenAI-compatible endpoint. |
| **LLM backup** | ENGR **H100 + `gpt-oss-120b`** kept | Free compute; switching is a base-URL flip. |
| **Compute** | **Cloud Run everywhere** — control plane + each agent as a scale-to-zero service | Free scale-to-zero, activation, and stable URL; no agent needs a GPU once LLM is on Vertex. |
| **GKE** | **Graduation path only** | Adopt when an agent declares a GPU/TPU need or needs strict isolation. |
| **Database** | **Cloud SQL for PostgreSQL** | Managed; replaces the `/engrfs` bind-mount. |
| **Registration svc** | Pinned Cloud Run singleton (`min=max=1`) | Owns lifecycle/loops; spawns agents via the Cloud Run API. Off the hot path. |
| **Login** | Keep **WashU Shibboleth**, repointed at the stable URL | Zero code change; preserves WashU SSO. |
| **Images** | **Artifact Registry** + GitHub Actions CI | Off the personal Docker Hub account. |
| **Slurm** | Stays as backup | De-risks the prototype. |

## Architecture

```
  Browser ─HTTPS─▶ WashU Shibboleth gateway ─▶ Cloud Run: PLANNER (autoscales)
                   (existing, repointed)             │ discover / dispatch
                          ┌─────────────────────────┼──────────────────────────┐
                          ▼                          ▼                           ▼
               Cloud Run: ADS           Cloud Run: REGISTRATION        Cloud Run: AGENTS
               (directory, min=1)       (singleton, min=max=1)         (1 svc/agent, min=0,
                          │                          │                  scale-to-zero per query)
                          └───────────┬──────────────┘                          │
                                      ▼                                          │
                            Cloud SQL (Postgres) ◀──────────────────────────────┘

   LLM: Vertex AI (managed Gemini)   [backup: ENGR H100 vLLM]
   Platform: Secret Manager · Artifact Registry · Cloud Logging · VPC (WashU-IP restricted)
```

### Component mapping (ENGR → GCP)

| Today (Slurm) | GCP |
|---|---|
| `srun` planner | **Cloud Run** (autoscales) |
| `srun` registration + host spawn-loop | **Cloud Run** singleton (spawn-loop bridge gone) |
| `srun` ADS daemon | **Cloud Run** (always-on) |
| Postgres on `/engrfs` | **Cloud SQL** |
| 6 agents as `srun` steps | **Cloud Run** service per agent, `min=0` |
| vLLM on Slurm GPU node | **Vertex AI** (backup: same vLLM) |
| Docker Hub `amaankhan25/*` | **Artifact Registry** + CI |
| `.env` + baked creds | **Secret Manager** |
| localhost:PORT on one node | **VPC**, WashU-IP-restricted ingress *(hardest part)* |

## Phases

Each is independently demoable; earlier phases de-risk later ones.

| # | Phase | Deliverable |
|---|---|---|
| **A0** | **Foundation** — GCP access (blocker), VPC + WashU-IP restriction, Cloud SQL, Artifact Registry, CI, budget alerts | Infra up; images pushed via CI |
| **A1** | **Control plane on Cloud Run** — planner/registration/ADS + Cloud SQL + Vertex, behind WashU login (agents still always-on) | End-to-end query works on GCP |
| **A1b** | **Load test** — k6/Locust via API, ~50 users; tune Cloud Run + Cloud SQL knobs | Known concurrency ceiling |
| **B2** | **Decouple publishing** — pre-publish the catalog independent of agent processes | Planner discovers a stopped agent |
| **B3** | **On-demand agents (milestone 1)** — `CloudRunBackend` + card resource field; per-query spin-up/teardown | One agent spun up per query, torn down |
| **B4** | **Fleet + LLM finalize** — remaining agents on-demand; email/geospatial Responses→Chat rewrites | Full fleet on-demand |
| **C5** | **Hardening** — A2A auth, Secret Manager, coding-agent sandboxing | Secured |
| **C6** | **Cutover eval** — GCP default once proven; **Slurm stays** | Parallel run, no files deleted |

## Expected cost

Rough list-price estimates (mid-2026, us-central1, single-region); verify with the GCP
Pricing Calculator. Credits apply to all of these (Vertex via Vertex, not AI-Studio).

| Service | Est./month |
|---|---|
| Cloud Run — planner + registration + ADS | ~$180 |
| Cloud SQL (small) | ~$10–30 |
| Vertex AI (Gemini Flash, ~2–5k queries) | ~$30–150 |
| Networking (LB) + Artifact Registry + egress | ~$25–30 |
| **Track A subtotal** | **~$250–350** |
| On-demand agents (Cloud Run, `min=0`) | pay-per-query (near $0 idle) |

**Runway:** ~$300/mo → the **$5k credits last ~15+ months**, well beyond the summer.
Main levers: Gemini **Flash** (≈4–8× cheaper than Pro), **stub the LLM during load tests**,
keep everything **single-region** (egress is the main hidden cost).

## Risks & open items

- **Networking is the hardest part** — VPC + WashU-IP restriction; scope early (A0).
- **Team ramp-up** — limited GCP experience; A0/A1 doubles as the learning curve.
- **Cold-start latency** on first per-query agent call — mitigate with a warm pool for hot agents.
- **email + geospatial** need a Responses→Chat rewrite (B4).
- **Durable-execution engines** (Temporal / Cloud Workflows) considered and **deferred** —
  revisit at B3 for guaranteed teardown + human-in-the-loop.

---
*Pricing sources: [Cloud Run](https://cloud.google.com/run/pricing) · [Cloud SQL](https://cloud.google.com/sql/pricing) · [Load Balancing](https://cloud.google.com/load-balancing/pricing) · [Vertex/Gemini](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing).*
