# Deployed components — what's live on GCP (updated 2026-07-29)

Operator reference for what's currently running on GCP (Phases 0-3: planner + real ADS + one
on-demand agent). Full design/status: [`GCP_MIGRATION.md`](GCP_MIGRATION.md) ("Shipped — Phases 0-3"
+ the "Operating the live planner" cheat-sheet). Everything is in project `di2-agenticweb`, region
`us-central1`.

## The request path

```
you ─(auth proxy)─▶ Cloud Run: planner ──dirctl──▶ ADS VM (10.128.0.3:8888)   [discover]
                          │                                                     [route ↓ via ID token]
                          │  LLM (primary → fallback)          Cloud Run: disaster agent (min=0)
                          ▼                                          │ cold-starts on demand
        vLLM-relay VM :8100 ─autossh─▶ h100-2405 (vLLM)             ▼ answers via Vertex
        (or) Vertex AI Gemini  ◀── automatic failover           back to planner → you
```

A message hits the planner; it **discovers** agents from the ADS directory, **routes** the task to the
chosen agent's Cloud Run URL (authenticated with a Google ID token), and reasons via **vLLM (primary)
with automatic Vertex fallback**. A `min=0` agent cold-starts on the call and scales back to zero.

## Components

**Cloud Run `planner`** — orchestrator + chat UI. Discovers agents via ADS (`dirctl` in-image,
`ADS_SERVER_ADDR=10.128.0.3:8888`), routes over authenticated A2A (Google OIDC ID token for `*.run.app`
targets), reasons via **vLLM-primary / Vertex-fallback** (`MAIN_AGENT_LLM_FALLBACK_*`). Private ingress
(reachable only via `gcloud run services proxy`). `min=1`, `--no-cpu-throttling` (60s health loop).

**Cloud Run `disaster` (on-demand agent)** — `min-instances=0`, so $0 idle; Cloud Run cold-starts it on
the planner's call. Runs `--no-publish-ads` (card is published decoupled); LLM on Vertex. The planner's
health probe skips it (`HEALTH_PROBE_SKIP_AGENTS`) so it actually scales to zero.

**`ads` VM (directory)** — Container-Optimized-OS GCE VM (`10.128.0.3`) running `dirctl daemon` + a
co-located `registry:2` OCI store (host networking, sqlite config `ads_daemon.gcp.yaml`). Ports
8888/8999 opened in the COS host firewall (+ startup-script for reboot persistence). Holds the routed
agent catalog; the disaster card was published to it decoupled via `ads_utils`.

**`vllm-relay` VM** — always-on e2-micro (`10.128.0.2`) holding an autossh tunnel (systemd) to
`h100-2405:8100` on the ENGR cluster — bridges GCP to the campus-firewalled vLLM. Dormant when the
planner is on the Vertex fallback.

**`planner-connector` (Serverless VPC connector, `10.8.0.0/28`)** — gives Cloud Run a path onto the VPC
to reach the ADS VM and the relay's private IPs. Firewall rules allow the connector range → `ads`
(8888/8999) and → `vllm-relay` (8100).

**Secret Manager** — `vllm-api-key` (the vLLM `--api-key`, mounted as the planner's `OPENAI_API_KEY`);
`disaster-card` (the mounted card, from the earlier fake-ADS phase — now superseded by the ADS record).

**ENGR vLLM (`gpt-oss-120b` on `h100-2405`)** — *not a GCP resource*; the primary LLM, reached through
the relay. Least-durable leg (Slurm job); Vertex fallback covers it when down.

## What's live vs. still to come
- **Live:** planner + real ADS discovery + authenticated A2A + one on-demand (scale-to-zero) agent, LLM
  with automatic failover.
- **Not yet:** Cloud SQL (persistence/credentials), WashU Shibboleth front door + internal-only ingress,
  the rest of the agent fleet, and **automated onboarding** (the `CloudRunBackend` — see
  [`DESIGN_CLOUDRUN_BACKEND.md`](DESIGN_CLOUDRUN_BACKEND.md) /
  [`IMPLEMENTATION_CLOUDRUN_BACKEND.md`](IMPLEMENTATION_CLOUDRUN_BACKEND.md)).

## Cost / pause
Always-on billers: planner (min=1), `ads` VM, `vllm-relay` VM, the connector. To pause: `min-instances=0`
on the planner + `gcloud compute instances stop ads vllm-relay` (but the disaster agent needs ADS up to
be discoverable). The connector can't be paused — delete/recreate for long idles.
