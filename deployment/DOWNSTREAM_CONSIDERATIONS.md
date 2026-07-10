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
duration of the Slurm job — `submit.sbatch` launches all 10 containers (Postgres,
ADS, registration, planner, and 6 specialists) in parallel and holds them up for the
whole 4-hour allocation. That is the right shape for a 6-agent demo on a single node, but it
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

## 2. Per-agent changes for cloud / scaled deployment

- **Status:** Open / Deferred — 2026-06-16
- **Raised:** 2026-06-16 (deployment review; companion to
  [`AGENT_INTEGRATION_SPEC.md`](AGENT_INTEGRATION_SPEC.md) §4–§5)
- **Scope:** every agent's LLM client, secrets handling, and served-model
  assumptions; `../<agent>/`, `dockerfiles/`, `.env.example`

**Problem.** The cross-cutting blockers — self-publish tied to process lifetime
(#1) and the shared substrate of DNS / auth / vLLM gateway / secrets (#3) — hit
every agent the same way. But each agent *also* carries its **own** current-system
dependencies that a cloud/scaled deploy must change individually: which LLM API it
speaks (hence whether it needs a code rewrite or just an endpoint swap), what
third-party secrets it holds, and what served-model capabilities (vision, web
grounding) it silently assumes. This entry tracks that **per-agent** work so no
agent's hidden coupling is forgotten when the fleet moves off the cluster. Anything
cross-cutting belongs in #1/#3; anything agent-specific belongs here.

**Current behavior — LLM API readiness matrix.** vLLM exposes an OpenAI-compatible
*Chat Completions* API. Agents already on Chat/litellm are endpoint-only swaps;
agents on the OpenAI *Responses* API (or Gemini) need a code rewrite.

| Agent | LLM API today | vLLM-ready? | Headline per-agent cloud work |
|---|---|---|---|
| planner | Chat Completions | ✅ done | stateless front door; drop dirctl-subprocess directory dep |
| disaster | Chat Completions | ✅ done (commit `55a7325`) | non-LLM only (publish decouple, DNS) |
| coding | litellm (Chat) | ✅ env-only | strong execution sandbox (YOLO bash) |
| intelligence | litellm (Chat) | ✅ env-only | stale card; `BRAVE_API_KEY` secret |
| scout | Chat Completions | ✅ env-only | **uses a different directory** (gRPC `:8889`) |
| email | **Responses** | ❌ rewrite | 4 call sites + 26 tool schemas; Google OAuth |
| geospatial | **Responses + Gemini** | ❌ hard | rewrite + vision + web-grounding gaps |

> ⚠️ CLAUDE.md §7's table row marking **disaster** as "Responses / rewrite needed"
> is **stale**: disaster was rewritten to Chat Completions (commit `55a7325`,
> `disaster_response_agent.py::build_response`) and already wires `OPENAI_BASE_URL`.
> Only **email + geospatial** remain on the Responses API. (CLAUDE.md §7 body text
> was partially updated; the table row was not.)

**Recommendation.**

- *HPC / now:* leave the Chat/litellm agents as env-only swaps; do **not** rewrite
  email/geospatial until self-hosted inference is the actual target — they work
  against hosted OpenAI today.
- *Future / at scale (per agent):*
  - **planner.** Already Chat-ready (`main_agent/llm_client.py`). For scale-out pass
    `base_url=` explicitly rather than relying on the SDK reading `OPENAI_BASE_URL`
    from env, and front the ephemeral vLLM node with a stable gateway (#3). Make it
    the **stateless, horizontally-autoscaled front door** (per-turn state lives on
    `TurnState`; it does not self-publish, so no lifecycle coupling). Replace its
    hard dependency on the local `dirctl` subprocess
    (`main_agent/directory/backends.py`) with a network/gRPC directory client so a
    pod needs no `dirctl` binary. Set a non-permissive `policy_mode` for a shared
    deployment, and note the bypass: with no skill hint, write-gating is skipped
    entirely (`runtime.py::_evaluate_policy`).
  - **email.** Migrate Responses → Chat Completions: 4 `client.responses.create`
    sites + ~26 tool schemas reshaped flat → nested, replace `previous_response_id`
    chaining with a locally-maintained message history, rewrite parsers
    (`response.output`/`output_text` → `choices[0].message`); use the planner's
    `llm_client.py` as the worked reference. Wire `base_url` into
    `email_agent.py::_openai_client` and relax its hard `OPENAI_API_KEY` guard to a
    dummy key (mirror disaster). Move Google OAuth creds **out of the image**
    (`Dockerfile.email` `COPY deployment/google_credentials/` → mounted secret /
    Workload Identity — the TODO is already in that Dockerfile). Externalize the
    long-lived per-user OAuth **refresh tokens** (currently a local-disk store) to a
    shared secret/DB so replicas and scale-to-zero restarts don't lose auth state.
  - **disaster.** LLM migration **done**. Remaining work is non-LLM: decouple its
    `atexit` self-publish/unpublish from process lifetime (#1) and advertise stable
    DNS instead of the hardcoded `10.229.117.223:9104` card URL. Optionally move its
    divergent self-publish helper (no read-back pull, no routing poll) onto the
    verified canonical path so its boot-time publish can't land stored-but-not-routed.
  - **geospatial — the hardest.** Migrate **both** Responses paths: the
    coordination/scene loop (`geospatial_agent.py`, with `previous_response_id`
    chaining) **and** the geolocation engine
    (`attack_bundle/src/pipeline_simple.py`). Force **both** provider selectors onto
    OpenAI/vLLM: `attack_bundle/src/config.py::detect_provider` (default model is
    `gemini-3-flash-preview`) and the inline `startswith(("gpt-","o"))` check in
    `geospatial_agent.py` — otherwise it routes to the non-OpenAI google-genai SDK.
    **Vision gap:** base64 `input_image` parts need a vision-capable served model
    (Qwen2-VL / Llama-3.2-Vision class) — likely a **separate GPU-served vision
    tier**; a text-only `gpt-oss-120b` loses image analysis entirely. **Web-grounding
    gap:** `web_search_preview` and Gemini `GoogleSearch` are not provided by vLLM —
    wire in a self-hosted search tool or lose grounding. Add a model env var (it has
    **none** in `.env.example`) and externalize `GOOGLE_API_KEY` / `GOOGLE_MAPS_API_KEY`.
  - **coding.** litellm, env-only: `OPENAI_API_BASE` + dummy `OPENAI_API_KEY` +
    `CODING_AGENT_PLANNER_MODEL=openai/<served-name>`, plus inject `api_base`/
    `api_key` into the inner mini-swe-agent `model_kwargs`
    (`coding_agent.py::_default_minisweagent_runner`) and set
    `MSWEA_COST_TRACKING=ignore_errors`. **Dominant concern is isolation:** it runs
    mini-swe-agent in YOLO mode (bash without confirmation); in multi-tenant cloud
    each request needs a strongly sandboxed/ephemeral execution environment
    (gVisor / Kata / Firecracker microVM or a per-request pod), not just a scratch
    workspace. It is also long-running (~25 s streaming heartbeat) — autoscaler / LB
    idle timeouts and scale-to-zero cold starts must tolerate long single requests
    and SSE keepalives. Note `coding_agent/_vendor_v2/` is a divergent protocol copy.
  - **intelligence.** litellm, env-only (`INTEL_AGENT_PLANNER_MODEL`). Decouple its
    `dirctl` self-publish/unpublish from process lifetime (#1); replace the vendored
    `dirctl`-CLI directory stack (`intelligence_agent/_vendor_v2/`) with a network
    client; externalize `BRAVE_API_KEY`. Resolve its `9105` port collision with
    geospatial via distinct cloud service names. Fix the stale card
    (`intelligence_agent.card.json` still says "mock deterministic" though it's a
    live LLM analyst).
  - **scout — directory reconciliation is the headline blocker.** It uses
    `agntcy_app_sdk` `AgentDirectory` over gRPC at `DIRECTORY_ENDPOINT` (default
    `127.0.0.1:8889`, `scout/network/directory.py`) while every other agent uses
    `dirctl` at `:8888`. Point scout's `AgentDirectory` at the same logical backend
    (or switch it to the shared registry) and **verify it becomes discoverable by
    the others.** Decouple its SIGINT/SIGTERM-handler unpublish (`scout/cli.py`);
    externalize `BRAVE_API_KEY`; advertise DNS via `SCOUT_PUBLIC_HOST`; and fix the
    stale `scout_agent/scripts/call_scout.py` (passes SLIM transport params the
    current factory rejects → `TypeError`).

- **Enablers already in the repo.** The planner's `main_agent/llm_client.py` is the
  Chat-Completions **reference implementation** for the email/geospatial rewrites;
  the disaster agent's already-migrated client (`OPENAI_BASE_URL` wiring + dummy-key
  guard) is a worked example of the endpoint side.

- **Open questions / risks.**
  - Which agents need a GPU-served **vision** model (geospatial) vs. can share the
    text LLM — a vision tier is its own deployment + cost.
  - Whether to standardize the fleet on **litellm** so endpoint swaps become uniform
    (vs. the current Chat/Responses/litellm/Gemini mix).
  - The `_vendor_v2/` divergence (coding, intelligence) means a publish-semantics fix
    must be replicated across copies (see CLAUDE.md §8).

- **References.**
  - `../email_agent/email_agent.py` (`_openai_client`, Responses call sites),
    `../geoSpatial_agent/geospatial_agent.py` + `attack_bundle/src/{config,pipeline_simple}.py`
  - `../planner_agent/main_agent/llm_client.py` (Chat-Completions reference),
    `../disaster_response_agent/disaster_response_agent.py` (`build_response`)
  - `../scout_agent/scout/{config.py,network/directory.py,cli.py}`
  - CLAUDE.md §7 (LLM/self-hosting), §8 (caveats); [`AGENT_INTEGRATION_SPEC.md`](AGENT_INTEGRATION_SPEC.md) §4

---

## 3. Cross-cutting cloud prerequisites (apply to every agent)

- **Status:** Open / Deferred — 2026-06-16
- **Raised:** 2026-06-16 (deployment review)
- **Scope:** every agent card's `url`/auth; `start_vllm.sbatch`; secrets handling;
  the directory backend(s); `.env.example`

**Problem.** A set of facts that are "fine on a trusted single node" become hard
requirements the moment the fleet runs in a shared cloud. These are neither
per-agent (#2) nor the activation model (#1) — they are the shared substrate every
agent inherits, so they are tracked once here rather than repeated per agent.

**Current behavior.**
- **Card URLs are LAN IPs / `127.0.0.1:<port>`** (single-node `localhost` reach),
  and inconsistent across copies (CLAUDE.md §3).
- **No auth:** cards have empty `securitySchemes`/`security`, agents bind `0.0.0.0`,
  and the OASF record's `auth` block is carried but never enforced (CLAUDE.md §8).
- **The vLLM endpoint is an ephemeral SLURM node hostname** pasted into `.env`
  (`OPENAI_BASE_URL` **and** `OPENAI_API_BASE`) every run.
- **Secrets are env-set or baked** (email Google creds `COPY`'d into the image).
- **Two directories coexist** — `dirctl` at `:8888` for most agents, scout's gRPC
  `AgentDirectory` at `:8889` — fronted by 5+ near-duplicate ADS-address env vars.

**Recommendation.**
- *HPC / now:* fine on a trusted LAN; don't add DNS/auth overhead for a 6-agent demo.
- *Future / at scale:*
  1. **Stable service DNS** for every card `url` (replace LAN-IP/`127.0.0.1`). This is
     a co-requisite of the activation proxy in #1, which owns the advertised URL.
  2. **Real A2A authn/authz** — populate `securitySchemes` and enforce at the planner
     and each agent before any shared-network exposure.
  3. **A stable vLLM gateway / reverse-proxy** in front of the ephemeral SLURM node;
     agents target the gateway, not the per-run hostname (CLAUDE.md §7
     "Ephemeral endpoint").
  4. **Externalize secrets** — mounted Secrets / secret manager / Workload Identity;
     no baked creds, no env-only API keys for shared infra.
  5. **Unify the directory** — reconcile scout's gRPC `AgentDirectory` (`:8889`) with
     the `dirctl` directory (`:8888`) onto one logical backend, and collapse the 5+
     duplicate ADS-address env vars.
  6. **Distinct service identities** — resolve the port collisions (intelligence /
     geospatial both `9105`; disaster's card / scout both `9104`) via service names,
     not the current 5-digit (`19107`/`19108`) re-maps.

- **Open questions / risks.**
  - Gateway/auth ownership on a time-boxed cluster — the same persistent-plane
    question as #1 (a Slurm job is not a long-lived orchestration plane).
  - Where auth lives: the planner front door, a service mesh/sidecar, or each agent.

- **References.**
  - CLAUDE.md §3 (hardcoded card URLs), §7 (ephemeral endpoint), §8 (no auth,
    scout's different directory)
  - `start_vllm.sbatch`, `.env.example`, [`AGENT_INTEGRATION_SPEC.md`](AGENT_INTEGRATION_SPEC.md) §4
  - Entry #1 (scale-to-zero) — the activation model that owns advertised URLs

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
