# Downstream Considerations

Forward-looking architecture and scaling decisions for the AgenticNetwork
deployment. This is **not** a bug tracker (use `HANDOVER.md` for known issues and
immediate next steps). Entries here capture design questions that are *fine for
now* but will matter as the system grows — the reasoning, the deliberate
deferral, and the path forward — so that a future maintainer inherits the
context, not just the conclusion.

## How to add an entry

Entries are numbered and append-only (don't renumber; mark superseded ones in
their **Status**). Copy the template at the bottom of this file. Each entry should
answer: *what's the issue, how does it work today, what should we do now vs.
later, and what already exists in the repo to help.* Keep the problem statement to
2–5 short paragraphs; use bullets for everything else. Reference code by
`path` and, where helpful, function name (line numbers drift).

---

## 1. On-demand agent activation (scale-to-zero)

- **Status:** Open / Deferred — 2026-06-15
- **Raised:** 2026-06-15 (deployment review)
- **Scope:** `deployment/submit.sbatch`, every agent's `serve-*` startup path, ADS publishing

**Problem.** The current deployment activates *every* agent container for the full
duration of the Slurm job — `submit.sbatch` launches all 8 containers (ADS,
planner, and 6 specialists) in parallel and holds them up for the whole 4-hour
allocation. That is the right shape for a 6-agent demo on a single node, but it
does not scale: with dozens or hundreds of agents we pay to keep idle services
(and their compute) running even when no request will ever reach them. The
natural target is **scale-to-zero** — keep the directory advertising every agent,
but only spin up a container when a call actually lands.

**Current behavior.** There is a chicken-and-egg obstacle baked into the code
today: each agent **publishes its own card to ADS on startup and unpublishes it on
shutdown** (see `serve-a2a` in `../disaster_response_agent/disaster_response_agent.py`,
which calls `publish_..._to_ads(...)` before serving and registers an `atexit`
hook to unpublish). So **a card's presence in the directory is tied to the
agent's process lifetime.** If we only start agents on demand, a stopped agent is
invisible to discovery → the planner never sees it → it never decides to call it →
it never gets activated. It cannot bootstrap itself.

Compounding this, an **A2A endpoint cannot activate its own agent.** The endpoint
*is* the running agent's HTTP server; if the container is down, nothing is
listening and the call simply fails with connection-refused. The protocol has no
wake-on-request behavior.

**Recommendation.**

- *HPC / now (current scale):* **Do nothing — don't overengineer.** Always-on is
  correct and simplest for ~6 agents on one node. Scale-to-zero also maps awkwardly
  onto batch HPC: "activate on demand" means either holding the Slurm allocation and
  launching new `srun --overlap` steps (fast) or submitting new jobs (subject to
  queue wait — seconds to hours under load), which would dominate any savings.
  Revisit only when agent count or idle cost becomes a real problem.
- *Future / at scale:*
  1. **Decouple publishing from the agent process.** Pre-publish every agent's
     card to ADS *persistently and independently* of whether the agent is running,
     so the directory always advertises the full catalog. The planner can then
     discover and choose a cold agent.
  2. **Add an activation layer.** Put a lightweight always-on **activation proxy**
     at each card's advertised `url`. On the first A2A request it (a) starts the
     container, (b) waits for readiness, (c) forwards the request, and (d) idles
     it back down after a timeout — the standard scale-from-zero pattern
     (Knative / KEDA / Lambda). Keep frequently-used agents warm; pre-pull/import
     images so cold starts pay seconds, not minutes.

- **Enablers already in the repo.** The decoupling primitive exists: a standalone
  publisher (`agent_directory_service/run_ads.sh -publish <card>` →
  `agent_directory_service/scripts/publish_agents.py`) can register a card without
  the agent running. The missing piece is the activation proxy, which is new
  infrastructure.

- **Clarification — "publishing a card" vs. an "ADS entry."** They are source →
  result, and publishing is *two* steps: `dirctl push` stores the card as a
  content-addressed OASF record (returns a CID) — **storage**; `dirctl routing
  publish <cid>` adds it to the routing index — **discovery**. A record can be
  **stored but not routed**, which makes it invisible to live discovery even
  though it exists. The planner discovers via *routing*, so any future
  pre-publishing scheme must ensure cards are **routed**, not merely stored. (This
  is why the `agent_directory_service/scripts/` repair/watch suite exists.)

- **Open questions / risks.**
  - Cold-start latency: large conda-env images take tens of seconds to minutes to
    pull/import; the first caller eats that delay unless images are pre-staged.
  - Lifecycle ownership: if publishing is decoupled, *what* keeps cards fresh and
    removes stale ones when an agent is permanently retired?
  - HPC fit: the proxy/scale-to-zero model assumes a long-lived orchestration
    plane, which a time-boxed Slurm job is not. May need a persistent gateway node.

- **References.**
  - `../disaster_response_agent/disaster_response_agent.py` — `serve-a2a` publish/unpublish on start/stop
  - `deployment/submit.sbatch` — all containers launched up front
  - `../agent_directory_service/run_ads.sh` (`-publish`), `../agent_directory_service/scripts/publish_agents.py` — standalone publisher
  - `../agent_directory_service/scripts/` — routing repair/watch tooling (stored-vs-routed)

---

## Entry template (copy for new entries)

```markdown
## N. <Short title>

- **Status:** Open / Deferred / In progress / Resolved / Won't-do / Superseded by #M — <date>
- **Raised:** <date> (<context>)
- **Scope:** <components / files affected>

**Problem.** <2–5 short paragraphs: what the issue is and why it matters at scale.>

**Current behavior.** <How it works today, with code references.>

**Recommendation.**
- *HPC / now:* <what to do at current scale — often "nothing, don't overengineer">
- *Future / at scale:* <the path forward, as concrete steps>

- **Enablers already in the repo.** <existing primitives that lower the cost — optional>
- **Open questions / risks.** <bullets — optional>
- **References.** <`path` + function names>
```
