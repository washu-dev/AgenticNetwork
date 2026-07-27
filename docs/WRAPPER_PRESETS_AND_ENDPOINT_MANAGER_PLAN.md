# Plan — Wrapper preset tools + endpoint "manager"

Status: **PROPOSAL / for review** (branch `feature/wrapper-presets-and-endpoint-manager`).
Nothing here is wired into a deployed service yet. This touches the custom-agent
creation system, which the team operates through the registration service — so
the approach in §4 should be agreed before implementation lands.

Goal (two asks):
- **A. LLM wrapper with preset tools** — today the "LLM Agent" option is
  system-prompt-only; let a creator include tools so the agent can *act*, not
  just chat.
- **B. Endpoint "manager"** — an optional LLM layer in front of an external
  endpoint that understands its I/O and translates to/from A2A both ways; long
  term, one manager fronts *multiple* endpoints.

---

## 1. The load-bearing context: there are TWO systems, not yet connected

What a creator sees today and what the engine can do are different code paths.

**System 1 — `registration_service/` (LIVE, what the UI offers).** FastAPI +
`static/index.html`; three `handler_type`s (`registration_service/models.py`):
- `llm` → `handlers/llm_handler.py`: a **single-turn** `chat.completions.create`
  (`llm_handler.py:79`), **no tool loop**, and it never passes `tools=`.
  `LLMConfig.tools: list[dict]` **exists but is dead** (`models.py:22`, never read).
  → this is exactly why "tool use isn't an option" today.
- `api_proxy` → `handlers/api_proxy_handler.py`: a **dumb pass-through** (wraps
  text in an A2A envelope, POSTs upstream, extracts text). **No LLM in the loop.**
- `custom` → upload a Python handler class; hosted by `agent_skeleton`'s
  `container_runner` + `HandlerExecutor`.

**System 2 — `agent_skeleton/` engine + `generator/` (richer, CLI-only today).**
A spec-driven *frozen* tool loop:
- `AgentSpec` (`agent_skeleton/spec.py:37`) = `system_prompt` + `tool_schemas` +
  `tool_registry` + `normalize` + `model` + `max_steps`.
- `run_tool_loop` (`agent_skeleton/llm_loop.py:66`) is the generic Chat
  Completions loop: passes `tools=tool_schemas`, reads `tool_calls`, dispatches,
  re-sends. **Only the spec changes; the loop never does.**
- `default_demo_spec()` (`spec.py:80`) — LLM + demo tools (`word_count`,
  `reverse_text`) **already tool-native**.
- `endpoint_wrapper_spec(...)` (`spec.py:100`) — one `call_endpoint` tool bound
  to an `EndpointConfig`; **already accepts `extra_schemas` / `extra_registry`**
  (`spec.py:109,127-128`) to add more tools.
- `generator/` emits a full agent folder (card + server + vendored engine) into
  the `agent-directory` repo, stamping the `oasf:<name>:<id>` routing tag
  (`generator/oasf.py:28`).

**Key fact:** the engine's own README lists *"Registration wiring — a new
`llm_endpoint` handler_type"* as **not-yet-done future work**. So both asks are
really "finish connecting System 2 into the creation flow," not "invent new
machinery." System 1's `llm`/`api_proxy` handlers are degenerate versions of
what System 2 already does properly.

**Canonical source of truth = `AgenticNetwork/agent_skeleton/`.** The
`agent-directory/.../wrapper_engine/` copies are byte-identical *vendored*
outputs of the generator (no `generator/` inside) — do NOT edit those; they are
regenerated. (Duplication risk per repo CLAUDE.md §9.)

---

## 2. Feature A — preset tools for the LLM wrapper

**Why:** a system prompt alone can't call anything. Tools turn the LLM wrapper
from a chatbot into an agent that can compute, look things up, or hit a helper.

**What (recommended shape):**
1. **A preset-tool catalog.** New `agent_skeleton/system_tools/<tool>.py`
   modules, each exporting a `(schema, fn)` or a `make_<tool>(cfg)` factory,
   modeled on `system_tools/call_endpoint.py:make_call_endpoint`. Add a
   name→factory index (e.g. `system_tools/catalog.py`). Start with 2–3 safe,
   dependency-light tools (e.g. `http_get`, `calculator`, `now`) so there is
   something real to select.
2. **A spec builder.** Add `llm_wrapper_spec(name, system_prompt, preset_tools:
   list[str], model=...)` in `spec.py`, parallel to `endpoint_wrapper_spec`. It
   resolves each name via the catalog and assembles `tool_schemas` +
   `tool_registry`. **No `AgentSpec` field change needed** — it already carries
   both lists, and `endpoint_wrapper_spec`'s `extra_*` params prove the pattern.
3. **Validation is free.** `validate_tool_registry` (`tools.py:59`) already
   enforces schema↔fn alignment; just feed it the assembled lists (called by
   `spec.validate()`).
4. **Generator input.** Add `preset_tools: list[str]` to `AgentAnswers`
   (`generator/models.py:34`) and thread it into the emitted `build_spec`; the
   catalog must be **vendored** (extend `ENGINE_IGNORE`/copytree in
   `repo_emitter.py`) so emitted agents can import the tool bodies.
5. **Surface it.** Two options (see §4 D1): route `handler_type=="llm"` through
   the engine, or add a new `llm_tools` type. Either way, populate the (already
   present) `LLMConfig.tools` / a new `preset_tools` field and add a selector to
   the `llm-fields` block in `static/index.html`.

**Hard parts:** tools with side effects or secrets (auth, per-user creds — reuse
the planner's credential-injection concept, don't invent a new secret path);
vendoring the catalog so emitted/containerized agents can import it.

---

## 3. Feature B — endpoint "manager" (I/O-aware, A2A both ways, eventually N endpoints)

**Why:** today one agent wraps exactly one endpoint, and "understanding I/O" is
just prose in the system prompt. A manager should (a) present a clean A2A face,
(b) know each endpoint's inputs/outputs well enough to call it correctly, and
(c) eventually route across several endpoints.

**What already exists (so we don't rebuild it):**
- **A2A *in*** — fully handled: `executor.py:_complete` (`:79`) returns the
  dual channel (structured `DataPart` + human text). No new work.
- **A2A *out*** — already handled: `call_endpoint` supports `protocol="a2a"`
  (posts a JSON-RPC `message/send`, extracts the reply — `call_endpoint.py:127`)
  and `protocol="http"`. So both an A2A upstream and a plain HTTP one are
  reachable today.

**What to add:**
1. **Multiple endpoints.** Generalize `endpoint_wrapper_spec` to accept
   `endpoints: list[EndpointConfig]` and register **one bound `call_endpoint`-style
   tool per endpoint** (distinct name/description) via the existing
   `extra_schemas`/`extra_registry` seam. The frozen loop already supports
   multi-tool selection — **no loop change**.
2. **I/O awareness.** Put each endpoint's input/output description into *its
   tool's* schema/description so the model picks the right endpoint and shapes
   the call. This is prompt/schema enrichment (extend `build_wrapper_prompt` /
   the generator's `io_criteria` per endpoint), not a control-flow change.
3. **Routing policy.** Default: LLM-chosen (the model reads the per-tool
   descriptions and calls the right one). Optionally later: skill-tag hints.
4. **Config/generator.** `AgentAnswers` grows an `endpoints: list[...]`; card
   can advertise one skill per endpoint or one umbrella skill.

**Hard parts:** per-endpoint auth env-var names (already env-name-based —
scales), tool-name disambiguation, larger prompt/schema surface. None require
editing the frozen loop.

---

## 4. Decisions to settle before implementing

- **D1 (architecture fork — biggest one):** How does the tool-capable LLM path
  reach users?
  - (i) **Route `handler_type=="llm"` through the System-2 engine** (reuse the
    tool loop; retire the single-turn `llm_handler`). *Recommended* — least new
    code, one loop.
  - (ii) Add a new `llm_endpoint`/`llm_tools` handler_type (the documented
    future work), leaving `llm` untouched.
  - (iii) Bolt a tool loop onto the thin `llm_handler.py` and read the dead
    `LLMConfig.tools`. *Not recommended* — grows a second loop to maintain.
- **D2 (tool source):** curated built-in catalog only, user-supplied JSON-schema
  tools (what the dead `LLMConfig.tools` implies), or both? Security posture for
  side-effecting/secret tools.
- **D3 (Feature B scope now):** ship single-endpoint-with-optional-manager first
  and add multi-endpoint after, or design for N endpoints from the start?
- **D4 (process):** this lives in `AgenticNetwork` (Amaan's repo) and the custom-
  agent creation flow he owns; it deploys via rebuilt Docker images, not repo
  edits. Agree the approach (esp. D1) with him before landing code.

---

## 4b. Implemented on this branch so far (Feature A, D1 = option i)

Decision **D1 = (i)**: the existing `llm` handler_type now runs through the shared
engine. Done + tested (stdlib-only, no LLM needed):
- **Preset-tool catalog** — `agent_skeleton/system_tools/preset_tools.py`: a
  `PRESET_TOOLS` index + `resolve_preset_tools(names) -> (schemas, registry)`,
  with two pure, safe starter tools (`calculator` via a safe AST evaluator,
  `current_time`). Network/secret tools deferred to D2.
- **Spec builder** — `agent_skeleton/spec.py::llm_wrapper_spec(...)`: creator's
  own prompt + optional preset tools; validates before returning; no tools ⇒
  prompt-only turn.
- **Routed `llm` through the engine** — `registration_service/handlers/llm_handler.py`
  now builds an `llm_wrapper_spec` and runs `agent_skeleton.llm_loop.run_agent`
  off-thread (dual-channel DataPart+text), replacing the single-turn call. The
  spec is built at startup so bad tool names fail fast.
- **Config + runner** — `LLMConfig.preset_tools: list[str]`
  (`registration_service/models.py`) threaded through `runner.py`.
- **Tests** — `agent_skeleton/tests/test_preset_tools.py` (11 checks). Existing
  `test_wrapper`/`test_generator` and `serve check` still green.

**Feature B (endpoint manager) — engine core done + tested** (D3 = design for N
endpoints from the start; a single-endpoint list is the degenerate case):
- **N named endpoint tools** — `EndpointConfig` gains `name` + `description`
  (`system_tools/call_endpoint.py`); `make_call_endpoint(cfg, tool_name=…,
  description=…)` now emits a per-endpoint-named tool (default unchanged, so the
  single-endpoint path is intact); `endpoint_tool_name()` derives a valid
  `call_<slug>` name.
- **Manager spec** — `spec.py::multi_endpoint_wrapper_spec(name, endpoints=[…])`
  registers one bound `call_<name>` tool per endpoint (via the existing seam),
  dedupes names, and validates. **The frozen loop is unchanged.**
- **Routing prompt** — `prompt.py::build_manager_prompt` lists each endpoint tool
  + its I/O so the model routes to the right one. A2A-in (executor dual channel)
  and A2A-out (`call_endpoint` `protocol="a2a"`) already existed.
- **Tests** — `agent_skeleton/tests/test_manager.py` (6 checks: naming, per-tool
  routing to the correct URL via localhost stubs, dedup, validation).

Not yet done (needs the LLM / cluster, or a later decision): the registration UI
tool-selector; generator support for `preset_tools` + `endpoints[]` + catalog
vendoring (§5 steps 3, 5); registration wiring so a creator can pick preset tools
/ define a multi-endpoint manager from the UI; a live end-to-end run against vLLM.

## 5. Suggested implementation order (once D1–D4 are settled)

1. Catalog scaffolding + 2–3 preset tools + unit tests (`agent_skeleton/tests`),
   using the stdlib-only, no-network test style already in the repo.
2. `llm_wrapper_spec` + validation smoke (`serve check`).
3. Generator: `preset_tools` in `AgentAnswers` + vendoring the catalog +
   `--print-card` dry run.
4. Registration wiring per D1 (route `llm` through the engine, or new type) +
   UI selector.
5. Feature B: `endpoints: list` in `endpoint_wrapper_spec` + per-endpoint tools +
   prompt/schema enrichment + generator support.
6. Docs: update `agent_skeleton/README.md` + this plan → "implemented".
