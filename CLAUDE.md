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
task. There are three kinds of thing in the repo:

- **One orchestrator** — the **planner** (`planner_agent/`). The "brain" you talk
  to. It has *no domain skills of its own*; it discovers other agents and
  delegates whole tasks to them. It is deliberately **agent-agnostic**: adding a
  new agent to the directory makes it usable with **zero planner code changes**.
- **Specialist agents** — live in the separate [`agent-directory`](https://github.com/washu-dev/agent-directory)
  repo: `email_agent`, `coding_agent`, `intelligence_agent`, `geoSpatial_agent`,
  `disaster_response_agent`, `scout_agent`. Each is its own HTTP service wrapping
  one capability.
- **A directory (ADS)** — `agent_directory_service/`. The registry/"phone book"
  where agents publish a card so the others can look them up by skill.

### The three contracts that hold it together

| Contract | Plain-English role | Implemented with |
|---|---|---|
| **Agent Card** (`*.card.json`) | An agent's résumé: name, URL, and a list of skills | local `card_oasf_converter.py` |
| **ADS** (the directory) | The phone book: publish your card, search by skill | AGNTCY `dir` via the `dirctl` CLI |
| **A2A** (agent-to-agent) | The phone call: send a task over HTTP/JSON-RPC, get a result | the official `a2a-sdk` |

---

## 2. Architecture

```
        You (browser / curl)
              │
              ▼
        ┌───────────┐    "who can send email?"    ┌─────────────┐
        │  PLANNER  │ ──────────────────────────▶ │  ADS / dir  │  (phone book)
        │  :8090    │ ◀────── card + endpoint ──── │ :8888       │
        └─────┬─────┘                              └─────────────┘
              │
              │ A2A call (HTTP/JSON-RPC): "send this email"
              ▼
   ┌──────────┬───────────┬─────────────┬──────────┐
   │  email   │  coding   │  geospatial │   …      │  specialist agents,
   │  :9103   │  :19106   │  :19105     │          │  each its own service
   └──────────┴───────────┴─────────────┴──────────┘
```

**Request lifecycle (end to end):**
1. A user message hits the planner's FastAPI server (`main_agent/server.py`,
   `POST /messages` or `/messages/stream`).
2. `MainAgentRuntime` (`main_agent/runtime.py`) runs a tool loop. The LLM
   (`main_agent/llm_client.py`) sees only **5 primitives** defined in
   `main_agent/tools.py`: `list_agents`, `describe_agent`, `call_agent`,
   `ask_user`, `final_answer`.
3. `list_agents`/`describe_agent` query ADS via `DirectoryClient`
   (`main_agent/directory/`), which shells out to `dirctl` (or uses an in-process
   fake in `--fake-ads` mode).
4. `call_agent` resolves the agent, checks policy, and dispatches the task over
   A2A (`main_agent/a2a/`) to the chosen specialist's HTTP endpoint.
5. The specialist's `AgentExecutor` runs its own LLM loop / API calls and returns
   a text summary plus a structured-data artifact.
6. The planner normalizes the result and the LLM emits `final_answer`; the server
   serializes the response (with a tool trace) back to the UI/caller.

**ADS has a load-bearing subtlety:** records are both *stored* (content-addressed,
`dirctl search`) **and** *routed* (a separate routing index, `dirctl routing
list/search`). A record can be **stored but not routed**, in which case it is
**invisible to live discovery**. The directory backend deliberately raises
`DirectoryUnavailable` rather than silently falling back to storage search. The
whole `agent_directory_service/scripts/` suite exists to detect and repair this.

---

## 3. Components & ports

**This repo (infrastructure):**

| Component | Role | Default port | Environment | LLM API surface |
|---|---|---|---|---|
| `planner_agent` | Orchestrator + chat UI | **8090** | pip venv (`requirements.txt`) | OpenAI **Chat Completions** (vLLM-ready; migrated 2026-06) |
| `agent_directory_service` | Directory (ADS) operator tooling | dir at **8888** (routing 8999) | conda `agent-directory-service` | n/a (`dirctl`) |
| `registration_service` | Agent self-registration web UI + manager | **8091** | pip venv (`requirements.txt`) | n/a |

**[`agent-directory`](https://github.com/washu-dev/agent-directory) repo (specialist agents):**

| Component | Role | Default port | Environment | LLM API surface |
|---|---|---|---|---|
| `email_agent` | Gmail/Calendar/Docs/Sheets | **9103** | conda `email-agent-a2a` (shared) | OpenAI **Responses** (gpt-5.2) |
| `scout_agent` | OSINT web research | **9104** | pip venv (`pyproject.toml`) | OpenAI **Chat Completions** |
| `geoSpatial_agent` | Photo geolocation + incident class. | **19105** | conda `email-agent-a2a` (shared) | Gemini + OpenAI **Responses** (multimodal) |
| `coding_agent` | Code writing via mini-swe-agent | **19106** | conda `coding-agent-a2a` | **litellm** (Chat) |
| `intelligence_agent` | Analyst / situational briefs | **19107** | conda `intelligence-agent-a2a` | **litellm** (Chat) |
| `disaster_response_agent` | Emergency response plans | **19108** | conda `email-agent-a2a` (shared) | OpenAI **Responses** (gpt-5) |

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
else runs on a laptop (Tiers A–C); Tier D is the containerized cluster deploy. There are four tiers:

### Tier A — Planner only, fake directory (pure laptop, no `dirctl`)
The planner has an in-process fake directory, so you need only Python + an API key.
```bash
cd /path/to/AgenticNetwork
python3 -m venv planner_agent/.venv
planner_agent/.venv/bin/python -m pip install -r planner_agent/requirements.txt
cp planner_agent/.env.example planner_agent/.env.local   # set OPENAI_API_KEY=sk-...
planner_agent/scripts/start.sh --fake-ads \
  --card ../agent-directory/email_agent/email_agent.card.json \
  --card ../agent-directory/coding_agent/coding_agent.card.json
```
Open **http://localhost:8090/** for the chat UI. (Agents are "discovered" from the
`--card` files; they only *execute* if actually running.)

### Tier B — Full local network (laptop, more setup)
- Install the **`dirctl`** binary (the only non-Python dependency; `brew tap
  agntcy/dir` or a release binary). `planner_agent/scripts/doctor.sh` checks it.
- Start the directory daemon + local OCI registry (`localhost:5000`). Scout
  bundles a `docker-compose.yml` that brings up a directory daemon.
- Start each agent on its own port, pointing at `localhost:8888`.
- Provide API keys per agent (OpenAI; `BRAVE_API_KEY` for scout/intelligence;
  Google OAuth for email).

### Tier C — ~~The lab LAN demo~~ (retired)
The original LAN demo on `10.229.117.*` is superseded by Tier D. Use Tier B for
full local testing or Tier D for cluster deployment.

### Tier D — ENGR cluster, containerized (as deployed)
`deployment/` runs the whole system on the WashU **Engineering (ENGR)** cluster via
Slurm + Pyxis/enroot. As of 2026-06 it is a **multi-container** setup: one Docker image
per service (`deployment/dockerfiles/Dockerfile.<agent>`, built/pushed by
`deployment/build_and_push.sh` to `amaankhan25/agenticnetwork-<agent>`), launched together
as parallel `srun --overlap` steps in `submit.sbatch` on one CPU node, plus a separate GPU
job (`start_vllm.sbatch`) serving `gpt-oss-20b` via vLLM. ADS starts first; each agent's
`entrypoints/entrypoint-<agent>.sh` waits up to 60 s for it, then `exec`s the agent with an
absolute conda path (`/opt/conda/envs/<env>/bin/python`). The planner reaches vLLM through
`OPENAI_BASE_URL`. Full runbook: `deployment/README.md`.

### Interfacing with the running system
- **Chat UI:** `http://localhost:8090/`
- **HTTP API:** `POST /messages` (JSON), `POST /messages/stream` (SSE),
  `GET /agents` (raw directory listing), `GET /health`.
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
disagree on library versions (ADS pins `a2a-sdk==0.3.2`, the planner uses
`0.3.26`). This is fine because **agents only ever communicate over A2A (HTTP)** —
they share no Python imports at runtime, so each can have any dependencies, any
Python version, even run on a different machine or container.

| Env | Components | How to create |
|---|---|---|
| pip venv | `planner_agent` | `python3 -m venv planner_agent/.venv && .../pip install -r planner_agent/requirements.txt` |
| pip venv (editable) | `scout_agent` | `python3 -m venv .venv && .venv/bin/pip install -e .` (needs Python ≥3.11) |
| conda `agent-directory-service` | `agent_directory_service` | `conda env create -f agent_directory_service/environment.yml` |
| conda `email-agent-a2a` (shared) | `email_agent`, `disaster_response_agent`, `geoSpatial_agent` | `conda env create -f disaster_response_agent/environment.yml` (the superset) |
| conda `coding-agent-a2a` | `coding_agent` | `conda env create -f coding_agent/environment.yml` |
| conda `intelligence-agent-a2a` | `intelligence_agent` | `conda env create -f intelligence_agent/environment.yml` |

`geoSpatial_agent/` ships **no** `environment.yml` of its own — it reuses the
shared `email-agent-a2a` env. The `email_agent` and `disaster_response_agent`
env files share the same name but are **not** identical: the geospatial deps
(`google-genai`, `geopy`, `Pillow`) live only in
`disaster_response_agent/environment.yml`, so create the shared env from that
(superset) file. OASF SDK packages come from the `buf.build` registry
(`--extra-index-url https://buf.build/gen/python`). Run each agent as its own
long-running process in its own activated environment.

---

## 6. How to build a new agent

Copy the simplest template (`disaster_response_agent/`) or the cleanest modern
one (`scout_agent/`). Five steps:

1. **Write an Agent Card** (`my_agent.card.json`): `name`, `url` (where it
   listens), `version`, and a `skills[]` list — each skill has a slash-namespaced
   `id` (e.g. `myagent/do_thing`), `description`, and `examples`. Tag
   side-effecting skills with `write`.
2. **Implement an executor** — a class extending the `a2a-sdk` `AgentExecutor`
   with `execute(context, event_queue)`. Read `context.get_user_input()`, do the
   work, return a text summary + a structured-data `DataPart` artifact (that's the
   channel the planner reads machine-readable output from).
3. **Serve it** — wrap the executor in `A2AStarletteApplication` +
   `DefaultRequestHandler` and run with uvicorn on a port.
4. **Publish to ADS on startup** — convert the card to an OASF record and `dirctl
   push` + `dirctl routing publish` it (copy a `publish_*_to_ads` helper from an
   existing agent). Unpublish on shutdown.
5. **Done** — the planner discovers it automatically on its next `list_agents`;
   no planner change needed.

Conventions: slash-namespaced skill ids; return structured output as a `DataPart`;
if work is slow, advertise `capabilities.streaming: true` and emit progress (the
coding agent uses a ~25s heartbeat to beat the caller's HTTP read timeout).

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
(`main_agent/llm_client.py`, commit `c4d8048` "added planner vllm support") and a
containerized ENGR deployment (`deployment/`, §4 Tier D) wires every agent to vLLM via
`OPENAI_BASE_URL`. The only agents still on the OpenAI **Responses** API are **email,
disaster, and geospatial** — so the planner web UI (:8090) now runs against self-hosted
gpt-oss-20b without a rewrite.

| Agent | API today | Endpoint wiring | Rewrite? | Difficulty |
|---|---|---|---|---|
| `scout_agent` | Chat Completions | env `OPENAI_BASE_URL` (works by accident; code edit optional) | No | 🟢 trivial |
| `coding_agent` | litellm (Chat) | env `OPENAI_API_BASE` + 1-line edit in `coding_agent.py` runner | No | 🟢 trivial–moderate |
| `intelligence_agent` | litellm (Chat) | env `OPENAI_API_BASE` only | No | 🟢 trivial |
| `planner_agent` | **Chat Completions** ✅ | env `OPENAI_BASE_URL` | **Done** — migrated 2026-06 (`main_agent/llm_client.py`) | 🟢 complete |
| `email_agent` | **Responses** | edit `_openai_client` in `email_agent.py` | **Yes** (4 call sites + 26 tool schemas) | 🟡 moderate |
| `disaster_response_agent` | **Responses** | edit `OpenAI()` ctor | **Yes** | 🟡 moderate |
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

**Responses → Chat Completions rewrite (email, disaster, geospatial-coordination — the planner is already done, use it as the reference):** the same shape of change in each:
- `client.responses.create(...)` → `client.chat.completions.create(...)`.
- **Replace `previous_response_id` chaining with a locally-maintained message
  history.** Responses keeps conversation state server-side by id; vLLM is
  stateless, so accumulate the full `messages` list (system + user +
  assistant-with-`tool_calls` + `{role:"tool", tool_call_id, content}`) and resend
  it each step.
- **Reshape tool schemas** from flat `{type, name, parameters}` (Responses) to
  nested `{type:"function", function:{name, parameters}}` (Chat). Email has ~26.
- Rewrite parsers to read `choices[0].message.tool_calls` / `.content` instead of
  `response.output` / `output_text`.
- `max_output_tokens` → `max_tokens`; drop Responses-only `reasoning=` and
  `text={verbosity}` (planner) — the UI "thinking" block then goes empty unless
  you parse gpt-oss's reasoning channel.

**Hard part — geospatial capability gaps (beyond the API rewrite):**
1. **Vision:** the geolocation pipeline sends images; a text model like
   `gpt-oss-20b` can't accept them. Needs a vision-capable served model
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
  --tool-call-parser <parser>` (the RIS example sbatch already sets these). Without
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

RIS docs (vLLM sbatch + inference examples) live in the sibling `ris-tips/llms-on-ris/` directory (outside this repo).

---

## 8. Authentication & per-user credentials

The planner (`:8090`) is protected by **WashU Shibboleth SSO**. Identity arrives
via Apache-injected headers (`eppn` / `mail` / `remote-user`); the planner never
handles passwords directly.

```
Browser → Apache (Shibboleth SP)
  → validates WashU SAML assertion
  → injects eppn/mail headers
  → proxies to planner :8090
```

**Key files:**
| File | Role |
|---|---|
| `main_agent/auth.py` | `get_shib_email()` — reads Shibboleth headers; `get_current_user()` — FastAPI dependency |
| `main_agent/auth_router.py` | `/auth/*` endpoints: logout, Google OAuth connect flow |
| `main_agent/credentials_router.py` | `/me/credentials/*` — store/list/delete per-user API keys |
| `main_agent/crypto.py` | AES-256-GCM encrypt/decrypt (`CREDENTIALS_ENCRYPTION_KEY` env var) |
| `main_agent/db.py` | Postgres pool; creates `users`, `sessions`, `user_credentials` tables |

**Per-user Google OAuth** — users can connect their Google account via
`GET /auth/google` → Google consent → `GET /auth/google/callback`. The resulting
`access_token`/`refresh_token` are AES-256-GCM encrypted and stored in
`user_credentials` under provider `google_oauth`.

**Admin access (P4f)** — admin is derived from the SSO identity: a user whose email
is in the `ADMIN_EMAILS` allowlist (comma-separated). `/admin/*` and the approver
endpoints use `require_admin` (401 if no identity, 403 if not allowlisted); fail
closed (empty list → no admins). The old shared `X-Admin-Token`/`MAIN_AGENT_ADMIN_TOKEN`
and `POST /admin/login` are retired. `/me` returns `is_admin` so the UI hides admin
surfaces for non-admins. Both the planner and `registration_service` read `ADMIN_EMAILS`
(dev/test: `registration_service` leaves it open under `FAKE_DB=1`).

**Credential injection** — at the start of each `/messages` request the planner
fetches + decrypts the requesting user's credentials and injects them into the A2A
task context. Specialist agents receive credentials in task context and remain
auth-unaware.

**What's not yet done** (auth model + roadmap: [`docs/CUSTOM_AGENT_LANE_V2_P4_PLAN.md`](docs/CUSTOM_AGENT_LANE_V2_P4_PLAN.md)):
- Credential migration: `BRAVE_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_MAPS_API_KEY`
  are still environment variables; the per-user injection path is built but agents
  don't yet read from context. See also the operator-owned vs per-user reconciliation
  in [`docs/CREDENTIAL_SOURCING_MODELS.md`](docs/CREDENTIAL_SOURCING_MODELS.md).
- Removing `deployment/google_credentials/` (still used by email agent).

(Google OAuth token refresh **is** implemented — `_refresh_google_token` in `server.py`.)

---

## 9. Important realities & caveats

- **Heavy code duplication, not a shared library.** The A2A + directory protocol
  layer exists in ~5 divergent copies: canonical `agent_directory_service/services/`,
  near-identical `_vendor_v2/` trees in `coding_agent/` and `intelligence_agent/`
  (identical to each other except the package `__init__.py` docstring; their
  import paths, error strings, and log path diverge from the canonical copy),
  a more-evolved fork in `planner_agent/main_agent/` (adds inline base64 file
  parts, optional skill, streaming progress, `demo-v3` records), and scout's own
  independent implementation. Library pins drift (`a2a-sdk` 0.3.2 vs 0.3.26). When
  fixing protocol bugs, check whether the fix needs replicating across copies.
- **Scout uses a different directory.** Every other agent uses `dirctl` at
  `10.229.117.37:8888`; scout uses `agntcy_app_sdk` `AgentDirectory` at
  `DIRECTORY_ENDPOINT` (default `127.0.0.1:8889`, gRPC). It is unverified whether
  these are the same logical directory — if not, scout may not be discoverable by
  the others.
- **Authentication is planner-only.** The planner (`:8090`) requires WashU
  Shibboleth login (see §8). However, specialist agent ports (9103, 9104, 19105–19108)
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
  `intelligence_agent.card.json` still says "mock deterministic" though the code is
  a live LLM analyst; `email_agent/notice_list.txt` is empty so `email/group_send`
  has no recipients; the email agent's self-publish skips the routing poll, so it
  can land in the stored-but-not-routed state.

---

## 10. Testing & validation

```bash
# Planner (no network, no ADS, no OpenAI key — uses ScriptedLLMClient + fake ADS)
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 planner_agent/.venv/bin/python -m pytest planner_agent/tests -q

# ADS package tests + import smoke (per AGENTS.md)
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 agent_directory_service/.venv/bin/python -m pytest agent_directory_service/tests -q
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.print_versions

# Planner end-to-end self-check (env, dirctl, ADS reachability, server, OpenAI, tests)
planner_agent/scripts/doctor.sh

# ADS operator commands (wrapper)
agent_directory_service/run_ads.sh -start | -status | -list | -diag | -repair | -watch | -publish <card>

# Validate an A2A endpoint without publishing
python -m agent_directory_service.scripts.validate_agent_endpoint \
  --card <card.json> --skill <skill> --prompt "..." --endpoint http://host:port/
```

---

## 11. Git, GitHub & OneDrive

- **Remote:** `origin` → `https://github.com/washu-dev/AgenticNetwork.git`,
  default branch `main`.
- **Local changes are local until pushed.** Edits in this folder (by you or by
  Claude) don't reach GitHub — "the collaborative version" — until `git add` +
  `git commit` + `git push`. Teammates see them after they `git pull`.
- **Claude does not commit or push unless explicitly asked.** When asked to make
  changes that should be shared, prefer a **feature branch + Pull Request** over
  committing to `main`.
- **OneDrive ≠ version control.** This repo lives in a OneDrive-synced folder.
  OneDrive backs up files to the cloud but is *not* how the team collaborates
  (that's GitHub), and syncing the hidden `.git/` directory can corrupt the repo
  or create conflict-copy files. Keep git operations the source of truth; ideally
  exclude `.git/` from OneDrive sync.
- `.DS_Store` files should be git-ignored (macOS noise).

---

## 12. Key file index

**This repo:**

| Path | What it is |
|---|---|
| `planner_agent/main_agent/runtime.py` | Orchestrator tool loop, ADS/A2A dispatch, policy, dedup |
| `planner_agent/main_agent/llm_client.py` | OpenAI **Chat Completions** client (the planner's LLM call; vLLM-compatible, migrated from Responses 2026-06) |
| `planner_agent/main_agent/tools.py` | The 5 tool schemas + system prompt |
| `planner_agent/main_agent/server.py` | FastAPI surface + chat UI serving |
| `planner_agent/main_agent/web/index.html` | Single-file chat UI |
| `planner_agent/scripts/start.sh` / `doctor.sh` | Launcher / self-check |
| `agent_directory_service/run_ads.sh` | ADS operator dispatcher |
| `agent_directory_service/services/directory/backends.py` | Real/fake ADS backends (dirctl) |
| `agent_directory_service/services/directory/card_oasf_converter.py` | Agent Card ↔ OASF record |
| `agent_directory_service/scripts/` | publish / show / diagnose / repair / watch ADS |
| `agent_directory_service/configs/ads_daemon_fast_routing.yaml` | Fast-routing daemon config |
| `agent_skeleton/` | Template for building new agents |
| `deployment/` | ENGR-cluster infra deploy: `dockerfiles/` + `entrypoints/` for planner/ADS/registration, `submit.sbatch`, `build_and_push.sh` |
| `registration_service/server.py` | FastAPI app — REST API for agent registration + static UI at `/` |
| `registration_service/db.py` | asyncpg pool + schema migrations; `FAKE_DB=1` swaps in `fake_db.py` |
| `registration_service/supervisor.py` | Subprocess lifecycle, ADS publish/unpublish, health loop; `FAKE_DB=1` swaps in `fake_supervisor.py` |
| `registration_service/fake_db.py` | In-memory drop-in for the asyncpg pool — used by `FAKE_DB=1` dev mode and tests |
| `registration_service/fake_supervisor.py` | No-op supervisor used by `FAKE_DB=1` dev mode and tests |
| `registration_service/tests/` | pytest suite (19 tests); run with `FAKE_DB=1 PYTHONPATH=. pytest registration_service/tests` |
| `AGENTS.md` | Project direction + working rules + validation |

**[`agent-directory`](https://github.com/washu-dev/agent-directory) repo:**

| Path | What it is |
|---|---|
| `ads_utils/converter.py` | Shared Agent Card → OASF/ADS record converter (used by all agents) |
| `ads_utils/publisher.py` | Shared `DirectoryPublisher` — canonical ADS publish/unpublish logic |
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
```
