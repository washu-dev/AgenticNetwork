# CLAUDE.md

Guidance for Claude (and humans) working in the **AgenticNetwork** repo. This
file explains *how the system fits together and how to run, extend, and operate
it*. For project direction and working rules see [`AGENTS.md`](AGENTS.md); for
the component index see [`README.md`](README.md). Each component also has its own
`README.md` with authoritative install/run steps.

---

## 1. What this is

AgenticNetwork is a demo of an **"Internet of Agents"**: a set of independent,
LLM-powered services that **discover each other through a shared directory** and
then **call each other over a standard agent-to-agent protocol** to complete a
task. The main pieces:

- **The chat app** (`chat_app/`) — the web service people sign in to and chat in: sign-in,
  the chat page, saved conversations, each user's encrypted keys, admin pages and the
  `/register/*` pass-through to the registration service.
- **One orchestrator** — the **planner**, the LLM loop inside the chat app (`chat_app/planner/`).
  The "brain" you talk to. It has *no domain skills of its own*; it discovers other agents and
  delegates whole tasks to them. It is deliberately **agent-agnostic**: adding a
  new agent to the directory makes it usable with **zero planner code changes**.
- **The registration service** (`registration_service/`) — where agent authors register
  agents (sign-up wizard + API). It also builds and runs those agents, on the `agent_skeleton`
  agent runtime, and publishes their cards to the directory.
- **Specialist agents** — live in the separate [`agent-directory`](https://github.com/washu-dev/agent-directory)
  repo: `email_agent`, `coding_agent`, `intelligence_agent`, `geoSpatial_agent`,
  `disaster_response_agent`, `scout_agent`. Each is its own HTTP service wrapping
  one capability.
- **A directory (ADS)** — AGNTCY's `dirctl` daemon. The registry/"phone book"
  where agents publish a card so the others can look them up by skill. This repo's client
  code is `agentnet/`, the one shared library the chat app and the registration service use for
  directory and A2A calls; it also holds the directory operator tooling.

### The three contracts that hold it together

| Contract | Plain-English role | Implemented with |
|---|---|---|
| **Agent Card** (`*.card.json`) | An agent's résumé: name, URL, and a list of skills | `agentnet/directory/card_oasf.py` |
| **ADS** (the directory) | The phone book: publish your card, search by skill | AGNTCY `dir` via the `dirctl` CLI |
| **A2A** (agent-to-agent) | The phone call: send a task over HTTP/JSON-RPC, get a result | the official `a2a-sdk` |

---

## 2. Architecture

```
        You (browser / curl)
              │
              ▼
        ┌───────────┐    "who can send email?"    ┌─────────────┐
        │  CHAT APP │ ──────────────────────────▶ │  ADS / dir  │  (phone book)
        │ (planner) │ ◀────── card + endpoint ──── │ :8888       │
        │  :8090    │                              └─────────────┘
        └─────┬─────┘
              │
              │ A2A call (HTTP/JSON-RPC): "send this email"
              ▼
   ┌──────────┬──────────┬─────────────┬───────────┬──────────┬──────────┐
   │  email   │  scout   │ geospatial  │  coding   │  intel   │ disaster │  specialist agents,
   │  :9103   │  :9104   │   :19105    │  :19106   │  :19107  │  :19108  │  each its own service
   └──────────┴──────────┴─────────────┴───────────┴──────────┴──────────┘
```

**Request lifecycle (end to end):**
1. A user message hits the chat app's FastAPI server (`chat_app/server.py`,
   `POST /messages` or `/messages/stream`).
2. `PlannerRuntime` (`chat_app/planner/runtime.py`) runs a tool loop. The LLM
   (`chat_app/planner/llm_client.py`) sees only **5 primitives** defined in
   `chat_app/planner/tools.py`: `list_agents`, `describe_agent`, `call_agent`,
   `ask_user`, `final_answer`.
3. `list_agents`/`describe_agent` query ADS via `DirectoryClient`
   (`agentnet/directory/`), which shells out to `dirctl` (or uses an in-process
   fake in `--fake-ads` mode).
4. `call_agent` resolves the agent, checks policy, and dispatches the task over
   A2A (`agentnet/a2a/`) to the chosen specialist's HTTP endpoint.
5. The specialist's `AgentExecutor` runs its own LLM loop / API calls and returns
   a text summary plus a structured-data artifact.
6. The planner normalizes the result and the LLM emits `final_answer`; the server
   serializes the response (with a tool trace) back to the UI/caller.

**ADS has a load-bearing subtlety:** records are both *stored* (content-addressed,
`dirctl search`) **and** *routed* (a separate routing index, `dirctl routing
list/search`). A record can be **stored but not routed**, in which case it is
**invisible to live discovery**. The directory backend deliberately raises
`DirectoryUnavailable` rather than silently falling back to storage search. The
whole `agentnet/scripts/` suite exists to detect and repair this.

---

## 3. Components & ports

**This repo (infrastructure):**

| Component | Role | Default port | Environment | LLM API surface |
|---|---|---|---|---|
| `chat_app` | Chat app: sign-in, chat UI, and the planner (orchestrator) inside it | **8090** | pip venv (`requirements.txt`) | OpenAI **Chat Completions** (vLLM-ready; migrated 2026-06) |
| `agentnet` | Shared directory (ADS) + A2A library, plus the settings, identity and credential-catalog code both services use; ADS operator tooling (`scripts/ads`) | dir at **8888** (routing 8999) | runs in the chat app and registration venvs | n/a (`dirctl`) |
| `registration_service` | Registration service: sign-up wizard + API for agent authors; builds, runs and monitors their agents and publishes their cards | **8091** | pip venv (`requirements.txt`) | n/a |

**[`agent-directory`](https://github.com/washu-dev/agent-directory) repo (specialist agents):**

| Component | Role | Default port | Environment | LLM API surface |
|---|---|---|---|---|
| `email_agent` | Gmail/Calendar/Docs/Sheets | **9103** | conda `email-agent-a2a` (shared) | OpenAI **Chat Completions** (vLLM-ready) |
| `scout_agent` | OSINT web research | **9104** | pip venv (`pyproject.toml`) | OpenAI **Chat Completions** |
| `geoSpatial_agent` | Photo geolocation + incident class. | **19105** | conda `email-agent-a2a` (shared) | Gemini + OpenAI **Responses** (multimodal) |
| `coding_agent` | Code writing via mini-swe-agent | **19106** | conda `coding-agent-a2a` | **litellm** (Chat) |
| `intelligence_agent` | Analyst / situational briefs | **19107** | conda `intelligence-agent-a2a` | **litellm** (Chat) |
| `disaster_response_agent` | Emergency response plans | **19108** | conda `email-agent-a2a` (shared) | OpenAI **Chat Completions** (vLLM-ready) |

> **5-digit ports (19105–19108)** are used for geospatial, coding, intelligence, and
> disaster to avoid collisions with other users on shared ENGR cluster nodes.
> Override via the `*_PORT` env vars in `deployment/.env` if needed.

> **Card URLs use `AGENT_HOST` placeholder or `127.0.0.1`.** The email, disaster,
> and geospatial cards use `http://AGENT_HOST:<port>/`; intelligence and coding use
> `127.0.0.1`. In the cluster, `AGENT_HOST` is resolved by the entrypoint. For
> local runs, use `--advertise-url` to override the published URL.

---

## 4. Running it

LLM inference is the only thing that needs real horsepower, and it's outsourced
to an API (hosted OpenAI/Gemini today, or self-hosted vLLM — see §7). Everything
else runs on a laptop: start with the dev stack. Tiers A–B are older component-level
setups, Tier D is the live ENGR cluster deploy and Tier E is GCP.

### Dev stack (recommended on a laptop)
`scripts/stack up` starts the chat app, the registration service, a seed agent, a mock LLM
and a local identity proxy, and returns when everything is ready. Open
**http://localhost:8080/app** (signed in as `dev@example.test`); `scripts/stack down` stops
it. No Docker, database, SSO or API key. Setup, a real LLM and adding agents:
[`docs/DEVSTACK.md`](docs/DEVSTACK.md).

### Tier A — Chat app only, fake directory (pure laptop, no `dirctl`)
The chat app has an in-process fake directory, so you need only Python + an API key.
```bash
cd /path/to/AgenticNetwork
python3 -m venv chat_app/.venv
chat_app/.venv/bin/python -m pip install -r chat_app/requirements.txt
cp chat_app/.env.example chat_app/.env.local   # set OPENAI_API_KEY=sk-...
chat_app/scripts/start.sh --fake-ads \
  --card ../agent-directory/email_agent/email_agent.card.json \
  --card ../agent-directory/coding_agent/coding_agent.card.json
```
Open **http://localhost:8090/app** for the chat UI. (Agents are "discovered" from the
`--card` files; they only *execute* if actually running.)

### Tier B — Full local network (laptop, more setup)
- Install the **`dirctl`** binary (the only non-Python dependency; `brew tap
  agntcy/dir` or a release binary). `chat_app/scripts/doctor.sh` checks it.
- Start the directory daemon + local OCI registry (`localhost:5000`). Scout
  bundles a `docker-compose.yml` that brings up a directory daemon.
- Start each agent on its own port, pointing at `localhost:8888`.
- Provide API keys per agent (OpenAI; `BRAVE_API_KEY` for scout/intelligence;
  Google OAuth for email).

### Tier D — ENGR cluster, containerized (the live deployment)
`deployment/` runs the whole system on the WashU **Engineering (ENGR)** cluster via
Slurm + Pyxis/enroot. As of 2026-06 it is a **multi-container** setup: one Docker image
per service (`deployment/dockerfiles/Dockerfile.<agent>`, built/pushed by
`deployment/build_and_push.sh` to `di2washu/agenticnetwork-<agent>`), launched together
as parallel `srun --overlap` steps in `submit.sbatch` on one CPU node, plus a separate GPU
job (`start_vllm.sbatch`) serving `gpt-oss-120b` via vLLM. ADS starts first; each agent's
own `entrypoint.sh` (in its directory in the `agent-directory` repo; this repo's
`deployment/entrypoints/` only holds the chat-app/postgres/registration ones) waits up to 60 s for it, then `exec`s the agent with an absolute
conda path (`/opt/conda/envs/<env>/bin/python`). The chat app's planner reaches vLLM through
`OPENAI_BASE_URL`. Full runbook: `docs/deployment/engr/README.md`.

### Tier E — GCP Cloud Run (built to a working milestone 2026-08; on hold since)
Cloud is a critical leg of the project going forward (decided 2026-10-13); the ENGR cluster
(Tier D) stays the live public deployment. The WashU GCP build
(`us-central1`) is **additive**; nothing on ENGR was deleted. **Live as of 2026-08-03
(re-verify):** the **chat app (Cloud Run service `planner`, renamed `chat-app` in code 2026-10-25) and registration service on Cloud
Run** wired to **Cloud SQL**, a **real ADS on a GCE VM**, authenticated cross-network A2A, LLM =
vLLM-primary with automatic **Vertex AI fallback**, and the **`CloudRunBackend`**
auto-deploying + auto-publishing registered agents end-to-end (register→build→deploy→publish). **Not built yet:** the public front door (the
chat app is anon + `gcloud run services proxy`-only) and the DNS cutover. If a task touches GCP,
start with the **GCP runbook** `docs/deployment/gcp/README.md` (architecture, how to access, dev
workflow, cluster differences); deeper dives: `docs/deployment/gcp/MIGRATION.md` (design,
status, gotchas), `.../DEPLOYED_COMPONENTS.md` (inventory + control commands), and
`.../DESIGN_CLOUDRUN_BACKEND.md` + `.../IMPLEMENTATION_CLOUDRUN_BACKEND.md` (the automation).

### Interfacing with the running system
- **Chat UI:** `http://localhost:8090/app` (public landing at `/`; `http://localhost:8080/app`
  in the dev stack)
- **HTTP API:** `POST /messages` (JSON), `POST /messages/stream` (SSE),
  `GET /agents` (raw directory listing), `GET /health`, `GET /ready` (per-dependency
  readiness), `GET /me`.
  ```bash
  curl -s http://localhost:8090/messages -H 'Content-Type: application/json' \
    -d '{"user_id":"demo","channel":"api","text":"What agents are available?"}' | jq
  ```
- **Direct to a specialist** (bypass the planner, for testing):
  `coding_agent/send_request.py`, `intelligence_agent/send_request.py`, the
  `scout` CLI (`scout "mission"` / `scout` REPL / `scout serve`), and each
  agent's local CLI mode (e.g. `disaster_response_agent.py respond "..."`).

---

## 5. Environments (each component is isolated)

**Different agents require different environments — by design.** Each component
has its own conda env or venv, and `README.md` states: *do not install one
component's requirements file into another component's virtual environment.* The environments even
disagree on library versions (registration pins `a2a-sdk==0.3.2`, the chat app uses
`0.3.26`). This is fine because **agents only ever communicate over A2A (HTTP)** —
agents share no Python imports at runtime, so each can have any dependencies, any
Python version, even run on a different machine or container. The two services in this
repo do share code in-process: both import `agentnet/` (so its tests run in both venvs), and
the registration service also imports `agent_skeleton` to run agents.

| Env | Components | How to create |
|---|---|---|
| pip venv | `chat_app` | `python3 -m venv chat_app/.venv && .../pip install -r chat_app/requirements.txt` |
| pip venv | `registration_service` | `python3 -m venv registration_service/.venv && .../pip install -r registration_service/requirements.txt` |
| pip venv (editable) | `scout_agent` | `python3 -m venv .venv && .venv/bin/pip install -e .` (needs Python ≥3.11) |
| pip venv (optional) | `agentnet` with `ADS_BACKEND=sdk` | `python3 -m venv agentnet/.venv && .../pip install -r agentnet/requirements-sdk.txt` (point `scripts/ads` at it with `ADS_PYTHON`) |
| conda `email-agent-a2a` (shared) | `email_agent`, `disaster_response_agent`, `geoSpatial_agent` | `conda env create -f email_agent/environment.yml` (the superset) |
| conda `coding-agent-a2a` | `coding_agent` | `conda env create -f coding_agent/environment.yml` |
| conda `intelligence-agent-a2a` | `intelligence_agent` | `conda env create -f intelligence_agent/environment.yml` |

`geoSpatial_agent/` ships **no** `environment.yml` of its own — it reuses the
shared `email-agent-a2a` env. The `email_agent` and `disaster_response_agent`
env files share the same name but are **not** identical: the geospatial deps
(`google-genai`, `geopy`, `Pillow`) live only in `email_agent/environment.yml`,
so create the shared env from that (superset) file — building it from
`disaster_response_agent/environment.yml` instead omits those deps and breaks
`geoSpatial_agent`. OASF SDK packages come from the `buf.build` registry
(`--extra-index-url https://buf.build/gen/python`). Run each agent as its own
long-running process in its own activated environment.

---

## 6. How to build a new agent

See [`docs/BUILDING_AGENTS.md`](docs/BUILDING_AGENTS.md): register one through the
registration service (`llm` / `proxy` with no code, or a `custom` upload), use the
`agent_skeleton` engine, or write a standalone A2A agent (card → executor → serve →
publish). The planner discovers it on its next `list_agents`; no planner change needed.

---

## 7. LLM configuration & self-hosting (RIS / vLLM)

LLM clients are configured per component (no shared LLM layer). The RIS cluster
hosts models with **vLLM** (`vllm.entrypoints.openai.api_server`), which exposes
an **OpenAI-compatible *Chat Completions* API** at `http://<node>:8100/v1/` —
**not** OpenAI's newer *Responses* API. That distinction drives all migration
work. Each migration has two parts: **endpoint wiring** (trivial — point the
client at the vLLM base URL with a dummy key + the served model name) and, for
the Responses-API agents, an **API rewrite** to Chat Completions.

**Update (2026-06):** the **planner is now migrated to Chat Completions**
(`chat_app/planner/llm_client.py`, commit `c4d8048` "added planner vllm support") and a
containerized ENGR deployment (`deployment/`, §4 Tier D) wires every agent to vLLM via
`OPENAI_BASE_URL`. **`email_agent` has since migrated too** (see table below) — the
only agent still on the OpenAI **Responses** API is **geospatial** — so the chat app
(:8090) now runs against self-hosted gpt-oss-120b without a rewrite.

| Agent | API today | Endpoint wiring | Rewrite? | Difficulty |
|---|---|---|---|---|
| `scout_agent` | Chat Completions | env `OPENAI_BASE_URL` (works by accident; code edit optional) | No | 🟢 trivial |
| `coding_agent` | litellm (Chat) | env `OPENAI_API_BASE` + 1-line edit in `coding_agent.py` runner | No | 🟢 trivial–moderate |
| `intelligence_agent` | litellm (Chat) | env `OPENAI_API_BASE` only | No | 🟢 trivial |
| `chat_app` (planner) | **Chat Completions** ✅ | env `OPENAI_BASE_URL` | **Done** — migrated 2026-06 (`chat_app/planner/llm_client.py`) | 🟢 complete |
| `email_agent` | **Chat Completions** ✅ | env `OPENAI_BASE_URL` | **Done** — already migrated (`email_agent.py`, `chat.completions.create`) | 🟢 complete |
| `disaster_response_agent` | **Chat Completions** ✅ | env `OPENAI_BASE_URL` | **Done** — migrated 2026-06 (`build_response`) | 🟢 complete |
| `geoSpatial_agent` + `attack_bundle` | **Responses + Gemini** | edit client ctors | **Yes** + capability gaps | 🔴 hard |

**Easy wins (no rewrite):**
- **scout** — `scout/agent.py` is already Chat-Completions-shaped and needs no
  change. The three `AsyncOpenAI(api_key=...)` constructions in `scout/cli.py`
  (`_run_one_shot`, `_run_legacy`, the serve flow) pass no `base_url`, but the
  OpenAI SDK reads `OPENAI_BASE_URL` from the env anyway — so setting
  `OPENAI_BASE_URL` + a dummy `OPENAI_API_KEY` + `--model <served-name>` works
  with **no code change** (adding an explicit `base_url=` is the optional, cleaner
  alternative).
- **coding / intelligence** — they use `litellm.acompletion`, which honors
  `OPENAI_API_BASE` / `OPENAI_API_KEY` for `openai/`-prefixed models. Env-only for
  the planner loops: set `OPENAI_API_BASE`, dummy `OPENAI_API_KEY`, and
  `CODING_AGENT_PLANNER_MODEL` / `INTEL_AGENT_PLANNER_MODEL=openai/<served-name>`.
  Coding's inner **mini-swe-agent** runner reads `model_kwargs` from a packaged
  YAML, so also inject `api_base`/`api_key` there (`_default_minisweagent_runner`
  in `coding_agent.py`) and set `MSWEA_COST_TRACKING=ignore_errors`.

**Responses → Chat Completions rewrite (geospatial-coordination — planner, disaster,
and email are already done; use them as the reference):** the same shape of change:
- `client.responses.create(...)` → `client.chat.completions.create(...)`.
- **Replace `previous_response_id` chaining with a locally-maintained message
  history.** Responses keeps conversation state server-side by id; vLLM is
  stateless, so accumulate the full `messages` list (system + user +
  assistant-with-`tool_calls` + `{role:"tool", tool_call_id, content}`) and resend
  it each step.
- **Reshape tool schemas** from flat `{type, name, parameters}` (Responses) to
  nested `{type:"function", function:{name, parameters}}` (Chat).
- Rewrite parsers to read `choices[0].message.tool_calls` / `.content` instead of
  `response.output` / `output_text`.
- `max_output_tokens` → `max_tokens`; drop Responses-only `reasoning=` and
  `text={verbosity}` (planner) — the UI "thinking" block then goes empty unless
  you parse gpt-oss's reasoning channel.

**Hard part — geospatial capability gaps (beyond the API rewrite):**
1. **Vision:** the geolocation pipeline sends images; a text model like
   `gpt-oss-120b` can't accept them. Needs a vision-capable served model
   (Qwen2-VL / Llama-3.2-Vision class) or the image-analysis function is lost.
2. **Web grounding:** uses OpenAI `web_search_preview` and Gemini `GoogleSearch`,
   neither of which vLLM provides — lost unless you wire in your own search tool.
3. The default geo path is **google-genai** (Gemini), a non-OpenAI SDK. There are
   **two** provider selectors to force onto OpenAI: the attack_bundle geolocation
   pipeline uses `config.detect_provider` (`attack_bundle/src/config.py`, default
   model `gemini-3-flash-preview`), while the agent's coordination/scene path uses
   an inline `startswith(("gpt-","o"))` check in `geospatial_agent.py`. Both must
   point at `gpt-`/`o` models to bypass Gemini.

**Cross-cutting gotchas:**
- **Tool calling requires vLLM launch flags** — `--enable-auto-tool-choice
  --tool-call-parser <parser>` (`deployment/start_vllm.sbatch` sets both). Without
  them, agents silently stop calling tools.
- **Model-name matching** — raw OpenAI clients send the model string verbatim, so
  it must equal vLLM's served name; litellm strips the leading `openai/`. Launch
  vLLM with `--served-model-name <simple>` and use that name everywhere.
- **Ephemeral endpoint** — the SLURM job is time-limited (`--time`) and the node
  hostname changes each run, so `http://<node>:8100` is not stable. For a
  persistent network, put a stable gateway/reverse-proxy in front, and tunnel
  (`ssh -L 8100:<node>:8100 ...`) to reach it off-cluster.
- **Relax dummy-key guards** — several agents hard-require `OPENAI_API_KEY`
  (planner `config.py` `load_config`, email `_openai_client` + the CLI guard,
  disaster `_call_model`). Set a non-empty placeholder for vLLM.
- **LLM client lifetime** — keep one async OpenAI client per endpoint for the life of the process
  and close it on shutdown; never build one per call and leave it for the garbage collector. The
  SDK's default async HTTP client closes itself from `__del__` by scheduling `aclose()` on
  whatever loop is running when it is collected; in the planner that late close was traced to an
  A2A `ConnectTimeout` on the first chat after a restart. `OpenAIToolLoopClient` caches one client
  per endpoint and event loop, and the app lifespan closes them. The skeleton's synchronous tool
  loop builds one client per run and closes it when the run ends.

`deployment/start_vllm.sbatch` is a working Slurm launch script for vLLM; for other launch flags see the vLLM documentation (https://docs.vllm.ai).

---

## 8. Authentication & per-user credentials

The chat app (`:8090`) is protected by **WashU Shibboleth SSO**. Identity arrives
via Apache-injected headers (`mail`, then `eppn`, then `remote-user`, read by
`agentnet/identity.py`); the chat app never handles passwords directly.

```
Browser → https://agenticweb.engr.wustl.edu   (public DNS — WashU ENGR)
  → nginx              (TLS termination, client_max_body_size limit)
  → Apache + mod_shib  (Shibboleth Service Provider)
      → validates the WashU SAML assertion (campus SSO)
      → injects eppn / mail / remote-user identity headers
  → reverse-proxy to the chat app :8090
```

> The nginx + Apache + Shibboleth **gateway is persistent infrastructure that lives
> OUTSIDE this repo** — a WashU ENGR host that holds the `agenticweb.engr.wustl.edu`
> DNS name and the Shibboleth SP registration. It reverse-proxies to the chat app,
> which runs on an **ephemeral Slurm compute node**, so the gateway's upstream must
> be repointed (or SSH-tunneled) to the current node each run. Stabilizing this is
> tracked in [`docs/deployment/DOWNSTREAM_CONSIDERATIONS.md`](docs/deployment/DOWNSTREAM_CONSIDERATIONS.md) #3.

In the dev stack, a local identity proxy (`devstack/dev_identity_proxy.py`) stands in for the
gateway: it strips client identity headers and injects `mail: dev@example.test`.

**Key files:**
| File | Role |
|---|---|
| `agentnet/identity.py` | Reads the identity headers and the `ADMIN_EMAILS` allowlist (both services) |
| `chat_app/auth.py` | `get_shib_email()` — the Shibboleth identity or 401; `get_current_user()` — FastAPI dependency |
| `chat_app/auth_router.py` | `/auth/*` endpoints: logout, Google OAuth connect flow |
| `chat_app/credentials_router.py` | `/me/credentials/*` — store/list/delete per-user API keys |
| `chat_app/store/crypto.py` | AES-256-GCM encrypt/decrypt (`CREDENTIALS_ENCRYPTION_KEY` env var) |
| `chat_app/store/db.py` | Postgres pool + schema: `users`, `user_credentials`, `agent_credential_grants`, `conversation_messages`, `pending_confirmations` (conversation history + pending confirmations moved off SQLite to Postgres — H2/H2b) |

**Per-user Google OAuth** — users can connect their Google account via
`GET /auth/google` → Google consent → `GET /auth/google/callback`. The resulting
`access_token`/`refresh_token` are AES-256-GCM encrypted and stored in
`user_credentials` under provider `google_oauth`.

**Admin access (P4f)** — admin is derived from the SSO identity: a user whose email
is in the `ADMIN_EMAILS` allowlist (comma-separated). `/admin/*` and the approver
endpoints use `require_admin` (the chat app: 401 if no identity, 403 if not
allowlisted, and its anonymous `demo-user` stand-in is never admin; the registration
service: 403); fail closed (empty list → no admins). The old shared
`X-Admin-Token`/`MAIN_AGENT_ADMIN_TOKEN` and `POST /admin/login` are retired. `/me`
returns `is_admin` so the UI hides admin surfaces for non-admins. Both the chat app and
`registration_service` read `ADMIN_EMAILS` through `agentnet/identity.py` (dev/test:
`registration_service` leaves it open under `FAKE_DB=1`).

**Credential injection** — at the start of each `/messages` request the chat app
fetches + decrypts the requesting user's credentials, and the planner injects the ones
each agent may have into the A2A task context. Specialist agents receive credentials in task context and remain
auth-unaware.

**What's not yet done** (auth model: [`docs/AUTH.md`](docs/AUTH.md); decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md)):
- Credential migration: `BRAVE_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_MAPS_API_KEY`
  are still environment variables; the per-user injection path is built but agents
  don't yet read from context. See also the operator-owned vs per-user reconciliation
  in [`docs/DECISIONS.md`](docs/DECISIONS.md) (D6).
- Removing `deployment/google_credentials/` (still used by email agent).

(Google OAuth token refresh **is** implemented — `_refresh_google_token` in `chat_app/server.py`.)

---

## 9. Important realities & caveats

- **Copies of the protocol code in the agents repo.** This repo has one A2A + directory
  package, `agentnet/`. The `agent-directory` repo has its own divergent copies:
  near-identical `_vendor_v2/` trees in `coding_agent/` and `intelligence_agent/`
  (identical to each other except the package `__init__.py` docstring; their
  import paths, error strings, and log path diverge from `agentnet/`),
  scout's own independent implementation, and the others listed in
  [`SECURITY.md`](SECURITY.md) §3. Library pins drift (`a2a-sdk` 0.3.2 vs 0.3.26). When
  fixing protocol bugs, check whether the fix needs replicating across copies.
- **Scout uses a different directory.** Every other agent uses `dirctl` at
  `ADS_SERVER_ADDR` (`localhost:8888` in the ENGR deploy; the ADS VM on GCP); scout uses
  `agntcy_app_sdk` `AgentDirectory` at `DIRECTORY_ENDPOINT` (default `127.0.0.1:8889`, gRPC).
  It is unverified whether these are the same logical directory — if not, scout may not be
  discoverable by the others.
- **Authentication is at the chat app only.** The chat app (`:8090`) requires WashU
  Shibboleth login (see §8), and the registration pages are reached through it. However, specialist agent ports (9103, 9104, 19105–19108)
  have no auth — agent cards have empty `securitySchemes` and bind `0.0.0.0`. On a
  shared node, any process on localhost can drive Gmail send, calendar/file writes,
  code execution, and geolocation directly. Write actions are gated only by the
  planner's optional `strict` policy mode when going through the planner.
- **`coding_agent` runs mini-swe-agent in YOLO mode** (bash runs without
  confirmation), contained only by a per-request scratch workspace — run it in a
  container/VM.
- **`geoSpatial_agent/attack_bundle/` is an offensive research tool** (DoxBench-
  style image geolocation/doxxing with privacy-leakage metrics) that doubles as
  the agent's production location engine. Be aware of its re-identification power.
- **Stale artifacts to watch:** `scout_agent/scripts/call_scout.py` passes SLIM
  transport params the current factory rejects (would `TypeError`);
  `email_agent/notice_list.txt` is empty so `email/group_send` has no recipients.

---

## 10. Testing & validation

All suites run from the repo root, offline (no Docker, Postgres, `dirctl` or API key).
`scripts/stack test` runs them, each in its own pytest process with the venv it needs
(`devstack/suites.py`); CI runs the same command per component in `.github/workflows/tests.yml`
(with `--python` and coverage summaries), plus a repo-hygiene job and the full-chain and scenario pipeline;
`.github/workflows/compose.yml` runs tier 2 (the Docker Compose smoke test) on PRs and pushes to
`main` touching deployment or service code, weekly and on demand. Each suite gets a clean
environment, so a stray `FAKE_DB=0` or `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` cannot leak in. When you
call pytest yourself, do not set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`: registration needs the
`pytest-asyncio` plugin, and most of its suite fails without it.

```bash
# Every suite: the unit suites, then the pipeline. Exits 0 when all pass, 7 when one fails,
# 3 when a venv or test plugin is missing (it names the fix).
scripts/stack test
scripts/stack test unit --parallel        # the unit suites at once
scripts/stack test --list                 # each suite with its path and interpreter
scripts/stack test registration_service -- -k approval -x   # after --: pytest's own args
# Coverage: add --cov (pytest-cov in the venvs); docs/TESTING.md.

# Chat app and its planner (no network, no ADS, no OpenAI key — uses ScriptedLLMClient + fake ADS)
scripts/stack test chat_app               # chat_app/.venv/bin/python -m pytest chat_app/tests -q -p no:cacheprovider

# Registration (registration_service/tests/conftest.py sets FAKE_DB=1), in the registration venv
scripts/stack test registration_service

# Agent skeleton (in-repo copy), in the registration venv
scripts/stack test agent_skeleton

# agentnet, the shared directory/A2A package (dirctl mocked). It runs in both service
# venvs (a2a-sdk 0.3.2 and 0.3.26), so this tests it with both.
# print_versions lists the deps of the venv running it.
scripts/stack test agentnet
registration_service/.venv/bin/python -m agentnet.scripts.print_versions

# Dev stack: mock LLM, fake dirctl, identity proxy, stack CLI safety checks, scripts/stack test
scripts/stack test devstack

# Full chain + scenarios (credential scoping, unreachable agent, proxy and preset-tool agents, step limit,
# ask_user, confirmation, restart): each file starts the dev stack on offset ports
# (PIPELINE_PORT_OFFSET, default 20000; the scenarios add 100) in its own state dir
# (.devstack-pipeline/, .devstack-scenarios/), drives it over HTTP only, then stops it.
# Needs both service venvs and lsof. One file: scripts/stack test full-chain (or scenarios).
scripts/stack test pipeline

# Tier 2 (Docker): the service images + real Postgres + real dirctl, via Docker Compose
# Its own project name and port, so `down -v` never touches a long-running Compose instance;
# `--rmi local` removes this project's images (<project>-<service>).
(cd deployment/compose && export COMPOSE_PROJECT_NAME=agenticnetwork-test FRONT_DOOR_PORT=18081 && docker compose up -d --build && docker compose run --rm seed && ./smoke.sh; docker compose down -v --rmi local)

# Chat app end-to-end self-check (env, dirctl, ADS reachability, server, OpenAI, tests)
chat_app/scripts/doctor.sh

# ADS operator commands (wrapper)
scripts/ads -list | -raw-list | -diag | -watch | -repair | -repair-delete | -publish <card> | -remove <cid> | -delete <cid>

# Validate an A2A endpoint without publishing
registration_service/.venv/bin/python -m agentnet.scripts.validate_agent_endpoint \
  --card <card.json> --skill <skill> --prompt "..." --endpoint http://host:port/
```

`scripts/stack` (`up`, `status --json`, `smoke`, `logs`, `add-agent`, `down`, `reset`,
`doctor`, `test`) is the one start and test command shared by people, agents, CI and the
full-chain test ([`docs/DEVSTACK.md`](docs/DEVSTACK.md)). `tests/pipeline/test_full_chain.py` runs a first-time
user's session against it: start from nothing → sign in → prompt → specialist answer via the
planner → saved history → register an agent → shut down with nothing left running.

---

## 11. Git & GitHub

- **Remote:** `origin` → `https://github.com/washu-dev/AgenticNetwork.git`,
  default branch `main`.
- **Local changes are local until pushed.** Edits in this folder (by you or by
  Claude) don't reach GitHub — "the collaborative version" — until `git add` +
  `git commit` + `git push`. Teammates see them after they `git pull`.
- **Claude does not commit or push unless explicitly asked.** When asked to make
  changes that should be shared, prefer a **feature branch + Pull Request** over
  committing to `main`.

---

## 12. Key file index

**This repo:**

| Path | What it is |
|---|---|
| `chat_app/planner/runtime.py` | The planner's tool loop, ADS/A2A dispatch, policy, dedup |
| `chat_app/planner/llm_client.py` | OpenAI **Chat Completions** client (the planner's LLM call; vLLM-compatible, migrated from Responses 2026-06) |
| `chat_app/planner/tools.py` | The 5 tool schemas + system prompt |
| `chat_app/server.py` | The chat app's FastAPI server: sign-in, chat UI, conversations, keys, admin routes, `/register/*` pass-through |
| `chat_app/web/index.html` | Single-file chat UI |
| `chat_app/scripts/start.sh` / `doctor.sh` | Launcher / self-check |
| `scripts/ads` | ADS operator dispatcher |
| `agentnet/directory/backends.py` | Real/fake ADS backends (dirctl) |
| `agentnet/directory/card_oasf.py` | Agent Card ↔ OASF record |
| `agentnet/directory/publisher.py` | `DirectoryPublisher`: the registration service's ADS publish/unpublish |
| `agentnet/a2a/` | A2A transport, adapters and client |
| `agentnet/scripts/` | publish / show / diagnose / repair / watch ADS |
| `deployment/ads_daemon.local.yaml` | Fast-routing local daemon config |
| `agent_skeleton/` | **Agent runtime** every hosted agent imports (LLM tool loop + `spec.py`, A2A executor/server, `AgentHandler`, `container_runner.py` image entry point, `system_tools/`) + a small demo agent (`demo/`: the only part safe to edit; the runtime never imports it, and it is not installed or vendored into images) + answers→agent **`generator/`** |
| `deployment/` | Infra **machinery** (shared images/entrypoints/launch scripts): `submit.sbatch`, `dockerfiles/`, `entrypoints/`, `build_and_push.sh`, `scripts/gcp_iam_step7.sh`. Deployment **docs** live in `docs/deployment/` |
| `docs/deployment/` | Deployment guides: `engr/README.md` (cluster runbook), `gcp/README.md` (GCP runbook) + `gcp/MIGRATION.md`, `gcp/DEPLOYED_COMPONENTS.md`, `gcp/DESIGN_`/`IMPLEMENTATION_CLOUDRUN_BACKEND.md`; agnostic: `AGENT_INTEGRATION_SPEC.md`, `DOWNSTREAM_CONSIDERATIONS.md` |
| `registration_service/server.py` | FastAPI app — REST API for agent registration + static UI at `/` |
| `registration_service/db.py` | asyncpg pool + schema migrations; `FAKE_DB=1` swaps in `fake_db.py` |
| `registration_service/supervisor.py` | Subprocess lifecycle, ADS publish/unpublish, health loop; `FAKE_SUPERVISOR` (defaults to `FAKE_DB`) swaps in `fake_supervisor.py` |
| `registration_service/fake_db.py` | In-memory drop-in for the asyncpg pool — used by `FAKE_DB=1` dev mode and tests |
| `registration_service/fake_supervisor.py` | No-op supervisor used by `FAKE_DB=1` dev mode and tests |
| `registration_service/tests/` | pytest suite; run from the repo root with `scripts/stack test registration_service` (its conftest sets `FAKE_DB=1`) |
| `devstack/` + `scripts/stack` | Native one-command dev stack (mock LLM, fake `dirctl`, identity proxy, seed agents); see `docs/DEVSTACK.md`. `scripts/stack test` runs every test suite (`devstack/suites.py`) |
| `tests/pipeline/` | Full-chain test driving the dev stack over HTTP (Goal 1 in `docs/OPEN_SOURCE_RELEASE.md`) |
| `.github/workflows/tests.yml` | CI: unit suites (with coverage), repo hygiene, full-chain + scenario pipeline |
| `.github/workflows/compose.yml`, `deployment/compose/` | Tier 2: Docker Compose stack and its smoke test ([`docs/deployment/COMPOSE.md`](docs/deployment/COMPOSE.md)) |
| `docs/OPEN_SOURCE_RELEASE.md` | Open-source release plan and status |
| `AGENTS.md` | Project direction + working rules + validation |

**[`agent-directory`](https://github.com/washu-dev/agent-directory) repo:**

| Path | What it is |
|---|---|
| `ads_utils/converter.py` | That repo's Agent Card → OASF/ADS record converter, used by its agents (this repo's copy is `agentnet/directory/card_oasf.py`) |
| `ads_utils/publisher.py` | That repo's `DirectoryPublisher` for its agents (this repo's canonical copy is `agentnet/directory/publisher.py`) |
| `email_agent/email_agent.py` | Email/Workspace agent (monolith; `ToolCallingEmailAgent`) |
| `coding_agent/agent_loop.py` | litellm tool loop |
| `coding_agent/_vendor_v2/` | Vendored A2A + directory stack for peer discovery |
| `intelligence_agent/agent_loop.py` | litellm tool loop |
| `intelligence_agent/_vendor_v2/` | Vendored A2A + directory stack for peer discovery |
| `geoSpatial_agent/attack_bundle/src/pipeline_simple.py` | Geolocation engine (vision + grounding) |
| `scout_agent/scout/` | Scout package (`agent.py` loop, `network/`, `capabilities/`) |
| `<agent>/<agent>.card.json` | Agent Cards (skills, endpoint, capabilities) |
| `<agent>/Dockerfile` | Per-agent Docker image (build from repo root) |
| `<agent>/entrypoint.sh` | Container entrypoint (waits for ADS, sets PYTHONPATH, starts agent) |
