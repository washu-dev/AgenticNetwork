# CLAUDE.md — agent_skeleton

Guidance for Claude (and humans) working **inside this package**. For the
human-facing walkthrough see [`README.md`](README.md); for the surrounding
system see the repo-root [`../CLAUDE.md`](../CLAUDE.md) and
[`../docs/deployment/AGENT_INTEGRATION_SPEC.md`](../docs/deployment/AGENT_INTEGRATION_SPEC.md).

---

## 1. What this is

The **agent runtime**: the engine every hosted AgenticNetwork **A2A agent** imports
at run time. It holds the LLM tool loop (`llm_loop.py`) and `AgentSpec`
(`spec.py`), the A2A executor and server (`executor.py`, `serve.py`), the
`AgentHandler` base class for custom agents (`base.py`, `handler_executor.py`),
`container_runner.py` (the entry point of every built agent image) and the system
tools (`system_tools/`). The registration service installs it into each agent's
environment and vendors it into each built image; the generator copies it into
its output as `wrapper_engine/`.

It also holds one small **demo agent** (`agent.card.json`, `tool_schemas.py`, the
demo tool bodies in `tools.py`, `SYSTEM_PROMPT` in `prompt.py`) and the
wrapper-repo **generator** (`generator/`). The engine was distilled from the
reference agent `disaster_response_agent.py` (now in the separate
[`agent-directory`](https://github.com/washu-dev/agent-directory) repo, where the
specialist agents live), split into small labeled files.

Two rules follow:

- **Don't build an agent by editing these files.** A change here reaches every
  hosted agent. Authors register one of three kinds instead
  ([`README.md`](README.md) "Building an agent";
  [`../docs/BUILDING_AGENTS.md`](../docs/BUILDING_AGENTS.md)): a `custom`
  `AgentHandler`, an `llm` agent with an uploaded tools module, or a no-code
  `proxy` / `llm` + `http` agent.
- **The import name `agent_skeleton` is a public interface.** Uploaded handlers
  import it, built images run `python -m agent_skeleton.container_runner`, and
  `registration_service/archive.py` rejects uploads that claim the name. Don't
  rename it or move `container_runner`.

## 2. The three contracts (recap)

| Contract | Role | Here |
|---|---|---|
| **Agent Card** (`agent.card.json`) | identity + skills + endpoint | `a2a-sdk` `AgentCard` model |
| **ADS** (directory) | publish/discover by skill | registration supervisor via `ads_utils/publisher.py`, or `publish_agents --card` by hand |
| **A2A** | the actual call (HTTP/JSON-RPC) | `execute()` in `executor.py` |

## 3. File map — engine, demo, tooling

The package serves **three lanes**; the tables below cover each. Lane A is the
spec-driven engine plus the demo agent; it is also what the generator vendors as
`wrapper_engine/`.

**Lane A — spec-driven engine + demo agent:**

| Zone | File | Part | Notes |
|---|---|---|---|
| 1 | `tool_schemas.py` | demo | `TOOL_SCHEMAS`: the demo's tools, Chat Completions shape |
| 2 | `prompt.py` | demo + engine | demo `SYSTEM_PROMPT`; engine `normalize_result()` (default for every spec without `normalize`, i.e. every hosted LLM agent) + `build_*_prompt()` |
| 3 | `llm_loop.py` | engine | generic loop; `run_agent()` runs a spec (demo spec if none) |
| 4 | `tools.py` | demo + engine | demo tool fns + `TOOL_REGISTRY`; engine `validate_tool_registry()` |
| 5 | `executor.py` | engine | `SkeletonAgentExecutor.execute()` + A2A I/O |
| 6 | `serve.py` | engine | `create_app`, CLI/uvicorn |
| — | `spec.py` | engine | `AgentSpec` seam: prompt+tools as data; `dispatch()`, the spec builders |
| — | `system_tools/` | engine | `call_endpoint` (endpoint-wrapper mode) + preset tools |
| — | `config.py` | engine | defaults for every agent (`MAX_TOOL_STEPS`, heartbeat `AGENT_NAME`, host/port, model, ADS addr) + env readers |
| — | `a2a_runtime.py` | engine | SDK import guard + `data_part`/`text_part`/`task_updater` |
| — | `agent.card.json` | demo | the demo's skills, url, version |

**Lane B — uploaded-code / custom-handler path** (what the registration service's
`custom` handler type runs; its `llm` type, and `container_runner`'s tool-loop
path, run Lane A's spec engine):

| File | Part | Notes |
|---|---|---|
| `base.py` | engine | `AgentHandler` (subclass + `handle_structured()`) + `FileInput` |
| `handler_executor.py` | engine | `HandlerExecutor` — wraps any `AgentHandler` in the A2A executor (heartbeat, runtime cap, credential context) |
| `container_runner.py` | engine | `python -m agent_skeleton.container_runner` — per-agent Docker entrypoint (loads handler, serves A2A) |

**Lane C — generator** (`generator/`): turns an `answers.json` into a complete
deployable wrapper-agent folder (card + server + Dockerfile + vendored
`wrapper_engine/`). Standalone CLI: `python -m agent_skeleton.generator`.

**Separation of concerns to preserve:** `executor.py` is the *only* file that
knows about A2A; `llm_loop.py` + `tools.py` + `prompt.py` are the "engine" and
know nothing about A2A. Keep it that way — it's what makes the agent testable
without a network.

## 4. How to add a tool (demo agent or an uploaded tools module)

These steps are written for the demo's files; changing the demo changes only the
demo. An author's uploaded tools module follows the same rules but keeps
`TOOL_SCHEMAS` and `TOOL_REGISTRY` in one file (`spec.tool_loop_spec` runs the same
check when the agent starts).

1. Add a schema to `TOOL_SCHEMAS` in `tool_schemas.py`.
2. Write the function in `tools.py` with **keyword args named exactly like the
   schema properties**; optional properties must have a default.
3. Add it to `TOOL_REGISTRY`.
4. `python -m agent_skeleton.serve check` — confirms the schema and function
   agree before you ever serve.

## 5. The alignment check (and closing the gap further)

`validate_tool_registry()` (in `tools.py`) is the safety net the specialist agents
lack — disaster matches tool names to functions by an `if`-chain with **no**
verification, so a mismatch fails silently at runtime. The check here verifies
the schema envelope (`"type": "function"`, `parameters.type` `"object"`; a missing
value only warns), name coverage (both directions), property↔parameter
correspondence, and that optional properties have defaults. It runs in every spec
builder (`AgentSpec.validate()`), in `create_app()` and as `serve check`.

**Stronger option — make drift impossible:** instead of hand-writing
`TOOL_SCHEMAS` *and* the function and checking they match, generate the schema
*from* a typed function (e.g. a `pydantic` model for the args →
`model_json_schema()`, or introspect type hints + docstring). Then there is one
source of truth and the schema cannot disagree. The current check is the
cheaper belt-and-suspenders version; both can coexist.

## 6. Gotchas (inherited from the parent system)

- **Stored-but-not-routed:** a card can be pushed to ADS yet not appear in the
  routing index → invisible to the planner. Publish through `ads_utils/publisher.py`
  (which polls routing) or `python -m agent_directory_service.scripts.publish_agents --card ...`.
- **Dummy key guards:** `OPENAI_API_KEY` must be non-empty even for vLLM; set a
  placeholder.
- **vLLM tool calling needs launch flags:** `--enable-auto-tool-choice
  --tool-call-parser <parser>`, or the model silently stops calling tools.
- **Model-name matching:** raw OpenAI clients send the model string verbatim;
  it must equal vLLM's `--served-model-name`.
- **Port collisions:** default is `9110`; the specialist agents use `9103-9104` and
  `19105-19108`, registered agents `19200-19299`. Reassign if co-hosting.
- **No auth:** binds `0.0.0.0` with empty `securitySchemes`. Same posture as the
  rest of the repo — fine for a demo LAN, not for exposure.

## 7. Testing

```bash
python -m agent_skeleton.serve check       # schema/function alignment (no deps beyond stdlib)
python -m py_compile agent_skeleton/*.py   # syntax
registration_service/.venv/bin/python -m pytest agent_skeleton/tests -q -p no:cacheprovider
# engine unit test (no a2a-sdk needed): import agent_skeleton.tools and call the fns
```

Because `executor.py`/`serve.py` degrade gracefully when `a2a-sdk` is missing
(the base class becomes `object`), you can import and test the engine and the
alignment check without installing the SDK; only *serving* requires it.

## 8. When extending the protocol layer

This runtime intentionally mirrors the disaster agent's A2A usage. If you fix a
protocol bug here, note that the parent repo has **~5 divergent copies** of the
A2A/directory stack (see [`../CLAUDE.md`](../CLAUDE.md) §9, Important realities & caveats) — a fix here does not
propagate to them.
