# Agent Integration Spec

**What information the system needs about an agent, and which dependency requires
each piece.** This is the reference for anyone *adding* an agent (by hand or via a
generator) and for anyone reasoning about how the current ENGR-cluster deploy
differs from a future cloud/scaled one.

It is a companion to three other docs — read them for the surrounding context:
- [`../CLAUDE.md`](../CLAUDE.md) — the system overview (architecture, ports, LLM config).
- [`README.md`](README.md) — how to build/run the cluster deploy.
- [`DOWNSTREAM_CONSIDERATIONS.md`](DOWNSTREAM_CONSIDERATIONS.md) — forward-looking
  scale/cloud decisions; **entries #1–#3 there are the authoritative "what changes
  at scale" companion to §4–§5 of this file.** This spec describes *what is*; that
  doc describes *what should change*.

> Code is referenced by `path` + symbol name. Line numbers drift, so they are
> avoided here on purpose — `grep` the symbol.

---

## 0. How to read this

The system's whole premise (see §7) is that **an agent is integrated as *data* — a
card in a directory — not as *code* in the orchestrator.** So "what the system needs
to know about an agent" is a concrete, enumerable list. §3 is that inventory. §4
maps each item to *why it exists* (which dependency requires it) and *whether it
survives* the two big migrations ahead (cloud, scale-to-zero). §5 is the practical
checklist for adding one agent today. §6 is the end-to-end lifecycle. §7 is the
fixed-vs-changeable boundary.

**Five dependency classes** are used throughout:

| Class | Meaning | Changes when… |
|---|---|---|
| **Intrinsic** | Any agent, any deployment, needs this — it's what "an agent" *is* | never |
| **AGNTCY-required** | Fixed by the A2A / `dir` / OASF framework; you conform to the shape, you don't own it | (only if AGNTCY changes) |
| **Creator-convention** | AgenticNetwork's own choice layered on AGNTCY's freeform fields; fully ours to change | whenever we decide |
| **Activate-all coupling** | Exists only because every agent is always-on and self-registers | **scale-to-zero** |
| **Cluster-specific** | Exists only because of Slurm / single-node / vLLM-on-HPC | **cloud deploy** |

---

## 1. The three contracts (one paragraph)

The system rests on three contracts. **(1) The Agent Card** (`*.card.json`) — an
agent's résumé: identity, endpoint URL, and a list of skills. The card schema is
**not** a repo-local schema; it is the `a2a-sdk` pydantic `AgentCard` model (cards
are loaded via `AgentCard.model_validate(...)`, e.g. in
`disaster_response_agent.py::create_disaster_a2a_app`), so that model is the
authority on what's required. **(2) The ADS directory** — agents `push` their card
(stored as a content-addressed OASF record → a CID) and `routing publish` it (added
to the routing index → discoverable). **(3) A2A** — the planner places an HTTP/
JSON-RPC `message/send` to an agent's URL and reads back a text summary plus a
structured `DataPart` artifact. The planner contains **no per-agent code** (§7), so
a new agent that publishes a valid card is callable with zero orchestrator change.

---

## 2. The information inventory at a glance

```
                         AGENT INFORMATION
   ┌─────────────────────────────────────────────────────────────┐
   │ 3a IDENTITY & ADDRESSING   name, url, version, protocolVersion │
   │                            provider/org · (derived: agent_id,  │
   │                            record_id, domains) · advertise-url │
   ├─────────────────────────────────────────────────────────────┤
   │ 3b CAPABILITIES (skills[]) id, name, desc, tags, examples,     │
   │                            inputModes/outputModes, security    │
   │                            · write/action_type/risk_level      │
   │                            · capabilities.streaming + heartbeat │
   ├─────────────────────────────────────────────────────────────┤
   │ 3c BEHAVIORAL CONTRACT     executor: execute()+cancel();        │
   │                            text summary + DataPart artifact     │
   ├─────────────────────────────────────────────────────────────┤
   │ 3d A2A INVOCATION ENVELOPE message/task/context ids; FilePart   │
   │                            kinds; advisory skill hint; per-skill │
   │                            task_input shape                     │
   ├─────────────────────────────────────────────────────────────┤
   │ 3e RUNTIME / OPERATIONAL   listen host:port; env vars; LLM      │
   │                            endpoint/model/API surface; secrets; │
   │                            conda env; self-publish hook         │
   ├─────────────────────────────────────────────────────────────┤
   │ 3f DIRECTORY PRESENCE      OASF record + CID + routing entry;    │
   │                            official OASF skill ids vs custom     │
   │                            tags; stored-vs-routed               │
   └─────────────────────────────────────────────────────────────┘
```

---

## 3. The canonical inventory — what the system needs to know

### 3a. Identity & addressing

| Field | Notes |
|---|---|
| `name` | Display name. **Load-bearing:** `agent_id` is derived from it (`card.name` lowercased + hyphenated, in `card_oasf_converter.py::oasf_record_to_discovered_agent`). Renaming the agent changes its `agent_id`. |
| `url` | Where the agent's A2A server listens / is reached. Becomes the ADS record's `interaction.endpoint` **and** the planner's dispatch URL. Today this is a LAN IP or `127.0.0.1:<port>` (see *advertise-url* below). |
| `version` | The agent's own release version (e.g. disaster `0.3.2`, email `0.2.6`). **Distinct from `protocolVersion`.** |
| `protocolVersion` | The A2A protocol version the card targets (`0.3.0` in all current cards). Required by the `AgentCard` model. |
| `description` | Free-text. Required by the `AgentCard` model. The planner shows it to the LLM in `list_agents`. |
| `provider` (optional) | `{organization, url}`. `organization` becomes the record's `organization` (defaults to `"unknown"`). |
| `preferredTransport` (optional) | `"JSONRPC"` in all cards. The planner hardcodes `transport="http"` for dispatch regardless. |

**Derived identifiers the system computes (you don't author these, but they govern
behavior):**
- `agent_id` = `name` lowercased + hyphenated.
- `record_id` = `uuid5(NAMESPACE_URL, "agent-directory-service:{name}:{version}")`.
  **Changing `name` *or* `version` produces a new `record_id`** — i.e. a new
  directory identity, which can orphan the old routing entry.
- `domains` = first slash-segment of each skill's custom tags (a discovery
  dimension the planner carries into `DiscoveredAgent`).
- `source_card_url` = `endpoint + /.well-known/agent-card.json` (synthesized).

**Advertise-url vs listen-bind (a real, live distinction):** agents *bind*
`0.0.0.0:<port>` but *advertise* a different `url` in their card. Some support an
override (e.g. email's `EMAIL_AGENT_A2A_URL` / `--advertise-url`). Today these
disagree: the cards advertise `9103/9104/9105`, but the deploy binds disaster on
`19107` and geospatial on `19108` (§5). The advertised URL is what the planner
dials — get it wrong and dispatch fails even though the agent is up.

### 3b. Capabilities — the `skills[]` list

Each skill object actually carries **eight** fields (all present in every shipped
card): `id`, `name`, `description`, `tags`, `examples`, `inputModes`,
`outputModes`, `security`.

- **`id`** — slash-namespaced (e.g. `disaster/response_plan`, `email/send`). **The
  id is also repeated as the first entry of that skill's `tags[]`** — a consistent
  convention (the id appears twice).
- **`inputModes`/`outputModes`** — MIME-ish strings (`text/plain`,
  `application/json`). A skill can *narrow* the card-level defaults (e.g.
  `intelligence/entity_tracking` advertises `application/json` only).
- **`security: []`** — present per-skill, always empty (no auth).
- **`examples`** — natural-language examples the planner shows to the LLM to help it
  pick the skill.

**Write / risk metadata is *computed*, not just tagged.** There are three
independent ways a skill becomes "write":
1. its `id` is in the hardcoded `WRITE_SKILL_IDS` set
   (`card_oasf_converter.py`, e.g. `email/send`, `email/docs/*`, `calendar/*`,
   `code/bug_fix`);
2. it carries a `"write"` tag;
3. its id/tags/name/description match `WRITE_SKILL_TERMS`.

At publish time the converter writes per-skill `action_type` and `risk_level` into
`policy.skill_policies`, plus a top-level `policy.write_requires_confirmation`. **A
second, independent producer** — the planner's `runtime.py::_is_write_skill`
(substring match on `WRITE_SKILL_TOKENS`: `send/create/update/delete/write/append/
replace/publish/execute/fix/modify`) — re-derives write-ness at policy-eval time.
> ⚠️ The `"write"` tag convention is **applied inconsistently** in the cards
> (most email write-skills lack it; intelligence uses no read/write tags; disaster
> hardcodes `action_type="read"` in its OASF record). Do not rely on the tag alone.

**`capabilities` object** — `{streaming, pushNotifications, stateTransitionHistory}`.
`streaming` is the consequential one: the planner reads it
(`a2a/transport.py`) to choose streaming vs unary dispatch. **Operationally it
means "this agent is slow":** the coding agent emits a ~25 s heartbeat to beat the
caller's HTTP read timeout. "Is this agent slow / does it stream progress" is
operational info, not just a boolean.

**Multimodal / binary inputs** — beyond the `inputModes` strings, the A2A layer
carries `FilePart` with raw `bytes` and a `kind ∈ {file,image,audio,video}`. The
planner injects image parts for the geospatial agent. "Accepts images/binary" is
distinct agent info from a text inputMode and couples to the *served model* (a
text-only model can't accept images — see §4 and `DOWNSTREAM_CONSIDERATIONS.md` #3).

### 3c. Behavioral contract — the executor

- The agent implements a class extending the `a2a-sdk` `AgentExecutor` with **both**
  abstract methods: `async execute(context, event_queue)` and `cancel(...)`.
- `execute` reads the request via `context.get_user_input()` **and** pulls a
  structured payload from `context.metadata` + message `DataPart`s.
- It returns through **two channels, on every terminal path** (success, failure,
  *and* input-required): a machine-readable **`DataPart` artifact** (`add_artifact`)
  **and** a human-readable text message — with the structured output *also*
  duplicated under the message metadata key `structured_output`. (The planner reads
  the first `DataPart` artifact, falling back to that metadata key.)
- The executor is wrapped in `A2AStarletteApplication` + `DefaultRequestHandler`
  (+ `InMemoryTaskStore`) and served with uvicorn.

> The card is validated by the `a2a-sdk` `AgentCard` model; **without `a2a-sdk`
> installed the executor base degrades to plain `object`** and serving raises at
> construction. The A2A wrapper is AGNTCY; the LLM engine behind `execute` is the
> agent's own (Chat Completions / litellm / Responses — see §4 and #2 downstream).

### 3d. The A2A invocation envelope (per-call, not card fields)

These are threaded per request and are "information the system needs *around* an
agent call":
- `message_id`, `task_id`, `context_id`, `reference_task_ids` (conversation/threading).
- `FilePart` kinds (text/data/file/image) for the parts in the message.
- the **advisory `skill` hint** — `call_agent` requires only `agent_id` +
  `task_input`; `skill` is "advice, not authoritative," and the agent self-reports
  the skill it actually used (`chosen_skill` in the structured output).
- the **per-skill `task_input` shape** — the real, load-bearing contract a caller
  must satisfy: `email/send` needs `to[]`, `subject`, `body`; calendar needs a
  title; search needs a date range. This is not in the card's machine schema; it
  lives in the skill `description`/`examples` and the system prompt, and the planner
  has agent-shaped shims (e.g. `_augment_email_task_input`) to fill gaps.

### 3e. Runtime / operational config

| Item | Notes |
|---|---|
| Listen `host:port` | Binds `0.0.0.0:<port>`. Default ports in §5. |
| **LLM endpoint** | `OPENAI_BASE_URL` (OpenAI-SDK agents) or `OPENAI_API_BASE` (litellm agents). |
| **LLM model var** | Each agent declares its *own*: `MAIN_AGENT_LLM_MODEL`, `INTEL_AGENT_MODEL`, `CODING_AGENT_MODEL`, `EMAIL_AGENT_MODEL`, `DISASTER_AGENT_MODEL`. **There is no geospatial model var in `.env.example`.** |
| **LLM API surface** | Chat Completions / litellm / **Responses** / Gemini — differs per agent and determines cloud-migration effort (§4, #2 downstream). |
| **Per-agent secrets** | `BRAVE_API_KEY` (scout, intelligence); `GOOGLE_CALENDAR_*` OAuth + on-disk refresh tokens (email); `GOOGLE_API_KEY` / `GOOGLE_MAPS_API_KEY` (geospatial). |
| **Directory address** | Set via **five+ near-duplicate vars** (`ADS_SERVER_ADDR`, `ADS_SERVER_ADDRESS`, `ADS_URL`, `DIRECTORY_ENDPOINT`, plus `CODING_AGENT_DIRECTORY_URL` / `INTEL_AGENT_DIRECTORY_URL`) — an artifact of divergent vendored protocol copies. **Scout uses a *different* directory** (`agntcy_app_sdk` `AgentDirectory` over gRPC, default `:8889`) than everyone else (`dirctl` at `:8888`). |
| **conda env** | `planner-agent`, `scout-agent`, `email-agent-a2a` (email **and** disaster, built from different YAMLs), `coding-agent-a2a`, `intelligence-agent-a2a`, `geospatial-agent` (pip, **no** `environment.yml`). |
| **Self-publish hook** | Whether/how the agent registers itself on boot and unpublishes at exit — see 3f and §4. |

### 3f. Directory presence (the OASF record)

- Publishing produces a **content-addressed OASF record** (a CID) in storage, then a
  **routing entry**. The card is embedded inside the record; the planner re-validates
  the full embedded `agent_card` for transport.
- **Real ADS routes on *official OASF numeric* skill ids, not the card's
  slash-namespaced ids.** Only `images_computer_vision/image_classification` (id
  `203`) is mapped in `KNOWN_OASF_SKILLS`; email gets hardcoded routing-hint ids
  (`EMAIL_ADS_ROUTING_SKILLS`); **every other skill stays as a `custom_skill_tag`**,
  and discovery search *post-filters* on those custom tags. This two-tier id system
  (AGNTCY official ids for routing; our slash ids for matching) is core "how the
  agent is discovered" info.
- **Stored ≠ routed.** A record can be pushed (stored) but not routed → invisible to
  live discovery. The planner discovers via the routing index, not storage search.
- The record carries an `auth` block (`security`, `security_schemes`,
  `required_scopes`) copied straight from the card — **carried but never enforced**
  (all empty today).
> The in-process `FakeDirectoryBackend` (`AGENT_NETWORK_FAKE_ADS=true`) is a plain
> in-memory dict with **no** push/route distinction — the stored-vs-routed subtlety
> exists *only* against real ADS.

---

## 4. Dependency map — why each item exists, and what survives

This is the heart of the document. For each information item: **who produces it**,
**which dependency class** it belongs to, and whether it survives **cloud** (Slurm →
k8s/containers), **scale-to-zero** (cold/on-demand agents), and **N replicas**
(horizontal scale with shared state).

| Information item | Producer | Class | Cloud? | Scale-to-0? | N-replicas? |
|---|---|---|---|---|---|
| `name`, `description`, `version`, `protocolVersion` | card author | Intrinsic / AGNTCY-required | ✅ | ✅ | ✅ |
| **`url` (endpoint)** | card author | Intrinsic to A2A | ❌ **LAN-IP/127.0.0.1 → service DNS** | ⚠️ proxy owns it | ⚠️ LB owns it |
| `agent_id` (= name) | converter (derived) | Creator-convention | ✅ | ✅ | ✅ |
| `record_id` (= name+version) | converter (derived) | AGNTCY/ADS key | ✅ | ⚠️ version bump = new record | ✅ |
| `provider`/`organization`, `domains` | author / derived | Creator-convention | ✅ | ✅ | ✅ |
| skill `id` (slash-namespaced) | card author | Creator-convention | ✅ | ✅ | ✅ |
| **official OASF skill id** (numeric) | converter mapping table | AGNTCY-required (routing) | ✅ | ✅ | ✅ |
| skill `description`/`examples`/`inputModes`/`outputModes` | card author | Intrinsic / AGNTCY-required | ✅ | ✅ | ✅ |
| **image/file `inputModes`** | card author | Intrinsic | ⚠️ **needs vision-capable served model** (lost under text-only vLLM) | ✅ | ✅ |
| write / `action_type` / `risk_level` | converter **and** planner (2 producers) | Creator-convention | ✅ | ✅ | ✅ |
| `capabilities.streaming` (+ heartbeat) | card author | AGNTCY-required | ✅ but LB idle-timeout must tolerate SSE | ⚠️ cold-start vs read-timeout | ✅ |
| executor (`execute`+`cancel`, dual-channel) | agent code | Intrinsic / AGNTCY-required | ✅ | ✅ | ✅ |
| A2A envelope (message/task/context ids) | runtime | AGNTCY-required | ✅ | ✅ | ⚠️ shared task store for N replicas |
| listen `host:port` | deploy env | Cluster-specific | ❌ pod port / service | ✅ | ✅ |
| LLM endpoint (`OPENAI_BASE_URL`/`_API_BASE`) | deploy env | Cluster-specific | ❌ **ephemeral SLURM node → stable gateway** | ✅ | ✅ |
| LLM model var (must == vLLM `--served-model-name` for raw OpenAI clients; litellm strips `openai/`) | deploy env | Cluster-specific | ⚠️ depends on inference tier | ✅ | ✅ |
| LLM API surface (Chat/litellm/**Responses**/Gemini) | agent code | Creator-convention | ❌ **Responses/Gemini agents need rewrite** (#2 downstream) | ✅ | ✅ |
| per-agent secrets (`BRAVE`, `GOOGLE_*`, OAuth tokens) | deploy env / baked image | Creator-convention | ❌ **baked → mounted secrets**; email OAuth tokens → shared store | ⚠️ email tokens not shared | ❌ email local-disk tokens break |
| directory address (5+ vars; scout's gRPC `:8889`) | deploy env | Cluster-specific | ❌ unify on one directory service; reconcile scout | ✅ | ✅ |
| **self-publish-on-boot + atexit/signal unpublish** | agent code | **Activate-all coupling** | ✅ | ❌ **the dominant blocker** — presence tied to process life (#1 downstream) | ⚠️ N replicas re-publish same record |
| OASF record / CID / routing entry | publish path | AGNTCY-required | ✅ | ⚠️ must stay *routed* when cold | ✅ |
| conda env / image | Dockerfile | Cluster-specific (conda detail) | ⚠️ image layers carry over; conda specifics may not | ✅ | ✅ |
| `auth` (empty securitySchemes/security) | card author | Creator-convention | ❌ **must add real authn/authz** before shared exposure | n/a | n/a |

**Cross-layer mappings worth calling out explicitly:**
- **`inputModes: image` → served-model capability.** An agent that accepts images
  *requires* a vision-capable served model. Under the shared `gpt-oss-20b` vLLM this
  capability is **lost** (geospatial). This is a dependency from an *agent info item*
  to the *LLM tier* — not expressible as a single class.
- **self-publish hook → `survives-scale-to-zero = NO` for every agent.** This is the
  single most important row. It is fully covered in `DOWNSTREAM_CONSIDERATIONS.md`
  #1; do not re-solve it here.
- **write classification has two independent producers** (converter at publish time;
  planner at eval time). A fix in one place does not change the other's behavior.
- **`url` is both** the ADS `interaction.endpoint` and the planner's dispatch target;
  it fails cloud (LAN IP) regardless of how the agent is otherwise wired.

---

## 5. Checklist — adding one agent to the current cluster deploy

Each artifact is tagged by dependency class so you know which survive the migrations.
The first three are the durable core; the rest are scaffolding.

| # | Artifact | Class | Survives cloud? | Survives scale-to-0? |
|---|---|---|---|---|
| 1 | **Agent Card** `*.card.json` (full required set: `name`, `description`, `url`, `version`, `protocolVersion`, `capabilities`, `defaultInputModes`, `defaultOutputModes`, `skills[]`; optional `provider`, `preferredTransport`, `securitySchemes`, `security`) | Intrinsic / AGNTCY | ✅ (rewrite `url`) | ✅ |
| 2 | **Executor** (`execute` + `cancel`; text + `DataPart` on all paths) | Intrinsic / AGNTCY | ✅ | ✅ |
| 3 | **Server wrapper** (`A2AStarletteApplication` + `DefaultRequestHandler` + uvicorn) | Intrinsic / AGNTCY | ✅ | ✅ |
| 4 | **Self-publish-on-boot + atexit/signal unpublish** | ⚠️ Activate-all | ✅ | ❌ moves to platform registrar |
| 5 | `dockerfiles/Dockerfile.<agent>` (two-stage: copy `dirctl` from `ghcr.io/agntcy/dir-ctl:v1.2.0`; base `continuumio/miniconda3`; mamba env; `ENTRYPOINT /entrypoint.sh`) | ⚠️ Cluster | ❌ rewritten | ✅ |
| 6 | `entrypoints/entrypoint-<agent>.sh` (wait ≤60 s for ADS; `exec` with **absolute** `/opt/conda/envs/<env>/bin/python`) | ⚠️ Cluster | ❌ → readiness probe | partial |
| 7 | `build_and_push.sh` `AGENTS=(…)` entry | ⚠️ Cluster | ❌ → CI | ✅ |
| 8 | `submit.sbatch` `srun --overlap` block | ⚠️ Cluster **+** Activate-all | ❌ rewritten | ❌ |
| 9 | `.env` entries (port, `*_MODEL`, endpoint, secrets) | Cluster/config | partial | ✅ |
| 10 | `environment.yml` (conda) — **not universal**: geospatial ships none (pip + `--extra-index-url buf.build`) | Intrinsic isolation; conda = Cluster | partial | ✅ |

**Reference ports** (cards advertise some of these differently — see *advertise-url*):

| Service | Port | | Service | Port |
|---|---|---|---|---|
| ADS dir | 8888 (routing 8999) | | coding | 9106 |
| vLLM | 8100 | | intelligence | 9105 |
| planner | 8090 | | disaster | **19107** (5-digit) |
| scout | 9104 | | geospatial | **19108** (5-digit) |
| email | 9103 | | | |

> Disaster's card says `9104` (collides with scout) and geospatial's says `9105`
> (collides with intelligence); the deploy re-maps them to 5-digit `19107`/`19108`
> to avoid collisions on the shared single node. Cards still advertise the old
> ports — a live inconsistency.

**Checklist gotchas (verified):**
- **Image-tag mismatch:** `submit.sbatch` pins `amaankhan25/agenticnetwork-ads:v4`,
  but `build_and_push.sh` builds/pushes everything with `TAG=latest`. The `ads:v4`
  tag the launcher references is **not produced by the build script**. The 7 agent
  steps use `:latest`.
- **ADS bring-up does *not* use `entrypoint-ads.sh`:** `submit.sbatch` overrides the
  command to `dirctl daemon start --config /workspace/deployment/ads_daemon.yaml`
  against the mounted config. `entrypoint-ads.sh` exists (the Dockerfile default) but
  uses `/tmp/ads` paths that contradict `Dockerfile.ads`'s own "avoid /tmp" comment.
- **Two poll timeouts, not one:** the `submit.sbatch` coarse poll waits **30 s**;
  each agent's `entrypoint-*.sh` retries up to **60 s**. (CLAUDE.md §4's "60 s" is the
  entrypoint figure.)
- **Update *both* endpoint vars each vLLM run:** `OPENAI_BASE_URL` (OpenAI-SDK agents)
  *and* `OPENAI_API_BASE` (litellm: intelligence, coding) share the `YOURNODE`
  placeholder. Updating only one strands the litellm agents.
- **OASF mapping decision:** does the new skill have an official OASF id (add it to
  `KNOWN_OASF_SKILLS`), or stay a custom tag (relies on post-filter matching)?
- **Advertise-url rewrite:** set the card `url` (or `--advertise-url`) to the address
  callers actually reach, not just the bind address.
- **Superseded artifacts — do not copy them:** `deployment/supervisord.conf`, the
  single-container `deployment/Dockerfile`, and `deployment/entrypoint.sh` are
  leftovers from the old single-container deploy. Use only `dockerfiles/` +
  `entrypoints/` + `submit.sbatch`.

---

## 6. End-to-end lifecycle (current cluster deploy)

**Create** → write card (1) + executor (2) + server (3) + self-publish hook (4) +
scaffolding (5–10).

**Build** → `build_and_push.sh` builds one image per agent (`AGENTS=(ads planner
scout email coding intelligence disaster geospatial)` → `amaankhan25/
agenticnetwork-<agent>`) and pushes to DockerHub. ADS = `debian:12-slim` + `dirctl`.

**Start vLLM** (separate GPU job) → `start_vllm.sbatch` serves `gpt-oss-20b` on
`:8100` with `--enable-auto-tool-choice --tool-call-parser openai
--reasoning-parser openai_gptoss`. The GPU node hostname is **ephemeral** → paste it
into `.env` (`OPENAI_BASE_URL` **and** `OPENAI_API_BASE`).

**Start the network** (`submit.sbatch`, one CPU node) →
1. `source .env` (with `set -a` so vars propagate to every `srun` step).
2. Start ADS first (`dirctl daemon start` against the mounted `ads_daemon.yaml`);
   poll `localhost:8888` up to **30 s**.
3. Launch all 7 agents as backgrounded `srun --overlap` steps running `/entrypoint.sh`.
4. Each entrypoint waits up to **60 s** for ADS, then `exec`s its server with an
   absolute conda path; the server **publishes its card on boot**.
5. `wait` — the job lives as long as the containers (4 h walltime).

**Publish** (per agent) → the **canonical verified path** (`RealADSDirectoryBackend`,
used by `run_ads.sh -publish` → `publish_agents.py`) is **three** dirctl calls:
`push <record> --output raw` → CID; `pull <cid> --output json` (read-back verify);
`routing publish <cid> --output json`; then poll `routing list --cid <cid>` until it
appears. **The agents' own boot-time self-publish helpers take a shortcut** — `push`
→ `routing publish`, **no read-back, no routing poll** — which opens a
**stored-but-not-routed window** if the routing reconciler lags. The repair/watch/
diagnose scripts in `agent_directory_service/scripts/` exist for exactly this.

**Discover & call** (a user prompt) →
`POST /messages` → planner runtime tool loop → LLM (vLLM) sees 5 tools →
`list_agents` (`dirctl routing search`/`list`, post-filtered on custom tags) →
`describe_agent` → `call_agent(agent_id, task_input, skill?)` resolves the endpoint
**from the card url** and sends A2A `message/send` over HTTP/JSON-RPC → the specialist
runs its own LLM loop and returns text + a `DataPart` artifact → `final_answer`.

**Decoupling primitive (for the future):** `run_ads.sh -publish <card>` →
`publish_agents.py` registers a card **without the agent running** — the seam that a
scale-to-zero design (#1 downstream) builds on.

---

## 7. Fixed (AGNTCY) vs. changeable (creators)

**You conform to (AGNTCY — the framework owns the shape):**
- the `a2a-sdk` `AgentCard` pydantic model — the **authoritative** card schema (there
  is *no* repo-local JSON schema; the converter's `validate_agent_card` only checks
  `name`/`url`/`skills`, a *weaker* check than the model);
- the A2A `message/send` + `Task`/`Artifact`/`Part` contract, the `AgentExecutor` base
  class, `A2AStarletteApplication`/`DefaultRequestHandler`;
- `dir`/`dirctl`: content-addressed `push` (CIDs), `routing publish/list/search`, the
  stored-vs-routed distinction; the OASF record schema + numeric skill taxonomy.

**You own (creators — change freely while staying within AGNTCY):**
- the entire planner runtime: the 5-tool design, system prompt, dedup/fingerprinting,
  **strict-mode write-gating** (a creator invention, *not* AGNTCY — and **off by
  default**, since `policy_mode` defaults to `permissive`), confirmation flow,
  streaming, the FastAPI surface + chat UI;
- the card→OASF converter and its skill-id mapping table; the directory backends
  wrapper (real/fake, routing-poll verification, stored-vs-routed detection);
- conventions layered on AGNTCY's freeform fields: slash-namespaced skill ids, the
  `write` tag, returning structured output as a `DataPart`;
- the `publish_*_to_ads` hooks, the operator scripts, **all** deployment scaffolding,
  and every agent's LLM client / model choice.

> The planner's "**zero per-agent code**" claim is true for *discovery and dispatch*
> (it resolves and calls any agent generically; the endpoint URL is never even shown
> to the LLM, and `call_agent` rejects raw URLs). The one qualifier: the runtime
> carries a few **agent-*shaped*** convenience shims keyed on skill-prefix/name
> substrings (email `task_input` augmentation; geospatial image injection) — these
> are *not* keyed on `agent_id` and do not break the zero-code-change-to-add-an-agent
> property, but a generator should be aware they exist.

---

## 8. Naming & identity foot-guns

- `agent_id` derives from `card.name` → **renaming changes the agent's id.**
- `record_id` derives from `name` **+** `version` → **a version bump creates a new
  directory record** (and can orphan the old routing entry).
- the slash-namespaced skill `id` is **duplicated** as the leading `tag`.
- `version` (agent release) **≠** `protocolVersion` (A2A protocol).
- **listen port ≠ advertised port** (disaster/geospatial bind 19107/19108 but cards
  say 9104/9105).
- real ADS routes on **official OASF numeric ids**, but matching post-filters on the
  card's **custom slash ids** — a skill with no official mapping is found only by the
  post-filter.

---

## 9. Key files

| Path | What |
|---|---|
| `<agent>/*.card.json` | the cards (identity, skills, endpoint) |
| `disaster_response_agent/disaster_response_agent.py` | cleanest executor + serve + self-publish template |
| `agent_directory_service/services/directory/card_oasf_converter.py` | Card ↔ OASF record; skill-id mapping; write-policy derivation |
| `agent_directory_service/services/directory/backends.py` | real/fake ADS backends; canonical publish + routing-poll; stored-vs-routed guard |
| `agent_directory_service/scripts/publish_agents.py`, `run_ads.sh` | standalone publisher (the decoupling primitive) |
| `planner_agent/main_agent/tools.py` | the 5 tools + system prompt |
| `planner_agent/main_agent/runtime.py` | tool loop, `call_agent`, policy/write-gating, agent-shaped shims |
| `planner_agent/main_agent/a2a/`, `directory/` | A2A transport + directory client (the more-evolved fork) |
| `deployment/dockerfiles/`, `entrypoints/`, `submit.sbatch`, `start_vllm.sbatch`, `build_and_push.sh`, `.env.example`, `ads_daemon.yaml` | the cluster scaffolding |
| `deployment/DOWNSTREAM_CONSIDERATIONS.md` | what changes at scale (#1 scale-to-zero, #2 per-agent cloud, #3 cross-cutting prerequisites) |
