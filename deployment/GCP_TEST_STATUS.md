# GCP Migration — Test Status (team update)

**As of 2026-07-14 · project `di2-agenticweb` · region `us-central1`**

Guiding principle unchanged: **GCP is additive.** The ENGR/Slurm deployment stays a
working backup; nothing is overwritten. Everything below is a parallel GCP path,
switched on by config, not a replacement. Detail: `GCP_MIGRATION_PLAN.md`.

---

## ✅ Tested & proven

### 1. Vertex AI LLM endpoint — reachable from the cluster
Validated the managed-Gemini path end-to-end from **both a laptop and an ENGR compute node**.

- **What works:** the OpenAI-compatible Chat Completions endpoint, model
  `google/gemini-2.5-flash` in `us-central1`, authenticated with a `gcloud` access token.
- **What it proves:** (a) an ENGR node has outbound egress to Google's APIs, (b) auth works,
  (c) the planner is a **base-URL + token swap** away from running on Vertex — no code rewrite.
- **This de-risks the LLM tier of the migration** (plan §4).

**Two findings worth knowing:**
- `gemini-2.0-flash-001` was **discontinued 2026-06-01** — we use `gemini-2.5-flash`.
- **Gemini-3.x models require the `global` endpoint** (not regional `us-central1`). That
  reopens the single-region/egress decision if we ever want a 3.x model — flagged in the plan.

---

## 🔄 In progress — next test

### 2. Decentralization test (runbook ready, not yet executed)
Deploy the **disaster-response agent** on GCP **Cloud Run**, publish its card into the
**live ENGR ADS**, and confirm the running agenticweb can **discover and route to it**.

- **Why this test:** it's the core "Internet of Agents" claim — an agent's location shouldn't
  matter. One passing run validates **four things at once**: hybrid discovery, cross-network
  A2A calling (ENGR → GCP), publishing decoupled from agent lifetime (the migration's hardest
  change), and the Vertex LLM leg (the agent thinks via Vertex, not ENGR).
- **Key design point:** the card is published **from ENGR** (pointing at the Cloud Run URL) —
  the GCP agent doesn't self-publish, because it can't reach ENGR's directory inbound. This is
  exactly the "decouple publishing" change the plan needs anyway.
- **The real question it answers:** can ENGR reach a GCP-hosted agent over the network? (Outbound
  to Google APIs is already proven; agent-to-agent routing is the next unknown.)

---

## Decisions holding
- **LLM:** managed Gemini via Vertex (primary); ENGR H100 + vLLM stays as backup (base-URL flip).
- **Compute:** Cloud Run per service, scale-to-zero for on-demand agents; GKE only if a GPU/
  isolation need appears.
- **Networking** remains the flagged hard part — inbound/VPC restriction is still unscoped
  (outbound is proven).

## Open / to note at meeting
- Which registry for GCP images: Artifact Registry (`di2-agenticweb`) vs current Docker Hub.
- Test agents run **unauthenticated + public** on Cloud Run — acceptable for scoped tests,
  must be torn down after; real deploy needs internal-ingress (plan §5).
