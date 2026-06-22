# CLAUDE.md — agent_skeleton

Guidance for Claude (and humans) working **inside this template**. For the
human-facing walkthrough see [`README.md`](README.md); for the surrounding
system see the repo-root [`../CLAUDE.md`](../CLAUDE.md) and
[`../deployment/AGENT_INTEGRATION_SPEC.md`](../deployment/AGENT_INTEGRATION_SPEC.md).

---

## 1. What this is

A small, faithful template for an AgenticNetwork **A2A agent**, distilled from
the reference agent
[`../disaster_response_agent/disaster_response_agent.py`](../disaster_response_agent/disaster_response_agent.py).
It exists to make the agent contract legible: the same ~70% of every agent that
is plumbing is isolated into "copy" files, and the ~30% that is the actual agent
is isolated into a few "write" files.

It is **not** wired into the repo's build/deploy yet — it's a starting point you
copy and rename (`agent_skeleton/` → `my_agent/`, `SkeletonAgentExecutor` →
`MyAgentExecutor`, the skill ids, the card).

## 2. The three contracts (recap)

| Contract | Role | Here |
|---|---|---|
| **Agent Card** (`agent.card.json`) | identity + skills + endpoint | `a2a-sdk` `AgentCard` model |
| **ADS** (directory) | publish/discover by skill | `dirctl push` + `routing publish` in `serve.py` |
| **A2A** | the actual call (HTTP/JSON-RPC) | `execute()` in `executor.py` |

## 3. File map — where to edit, where not to

| Zone | File | Status | Notes |
|---|---|---|---|
| 1 | `tool_schemas.py` | **WRITE** | `TOOL_SCHEMAS`: Chat Completions shape |
| 2 | `prompt.py` | **WRITE** | `SYSTEM_PROMPT` + `normalize_result()` |
| 3 | `llm_loop.py` | copy | generic loop; `run_agent()` wires zones 1/2/4 |
| 4 | `tools.py` | **WRITE bodies** | tool fns + `TOOL_REGISTRY` + `validate_tool_registry()` |
| 5 | `executor.py` | copy | `SkeletonAgentExecutor.execute()` + A2A I/O |
| 6 | `serve.py` | copy | `create_app`, ADS publish, CLI/uvicorn |
| — | `config.py` | edit defaults | name, host/port, model, ADS addr |
| — | `a2a_runtime.py` | copy | SDK import guard + `data_part`/`text_part`/`task_updater` |
| — | `agent.card.json` | **WRITE** | skills, url, version |

**Separation of concerns to preserve:** `executor.py` is the *only* file that
knows about A2A; `llm_loop.py` + `tools.py` + `prompt.py` are the "engine" and
know nothing about A2A. Keep it that way — it's what makes the agent testable
without a network.

## 4. How to add a tool (the loop the user cares about)

1. Add a schema to `TOOL_SCHEMAS` in `tool_schemas.py`.
2. Write the function in `tools.py` with **keyword args named exactly like the
   schema properties**; optional properties must have a default.
3. Add it to `TOOL_REGISTRY`.
4. `python -m agent_skeleton.serve check` — confirms the schema and function
   agree before you ever serve.

## 5. The alignment check (and closing the gap further)

`validate_tool_registry()` (in `tools.py`) is the safety net the real agents
lack — disaster matches tool names to functions by an `if`-chain with **no**
verification, so a mismatch fails silently at runtime. The check here verifies
name coverage (both directions), property↔parameter correspondence, and that
optional properties have defaults. It runs in `create_app()` and as `serve check`.

**Stronger option — make drift impossible:** instead of hand-writing
`TOOL_SCHEMAS` *and* the function and checking they match, generate the schema
*from* a typed function (e.g. a `pydantic` model for the args →
`model_json_schema()`, or introspect type hints + docstring). Then there is one
source of truth and the schema cannot disagree. The current check is the
cheaper belt-and-suspenders version; both can coexist.

## 6. Gotchas (inherited from the parent system)

- **Stored-but-not-routed:** a card can be pushed to ADS yet not appear in the
  routing index → invisible to the planner. `serve.py`'s `publish_card_to_ads`
  polls `routing list` to catch this (disaster does *not*).
- **OASF record fidelity:** `build_ads_record` is minimal. For real routing,
  prefer `python -m agent_directory_service.scripts.publish_agents --card ...`
  or the canonical `card_oasf_converter`.
- **Dummy key guards:** `OPENAI_API_KEY` must be non-empty even for vLLM; set a
  placeholder.
- **vLLM tool calling needs launch flags:** `--enable-auto-tool-choice
  --tool-call-parser <parser>`, or the model silently stops calling tools.
- **Model-name matching:** raw OpenAI clients send the model string verbatim;
  it must equal vLLM's `--served-model-name`.
- **Port collisions:** default is `9110`; the parent repo uses `9103-9106`.
  Reassign if co-hosting.
- **No auth:** binds `0.0.0.0` with empty `securitySchemes`. Same posture as the
  rest of the repo — fine for a demo LAN, not for exposure.

## 7. Testing

```bash
python -m agent_skeleton.serve check       # schema/function alignment (no deps beyond stdlib)
python -m py_compile agent_skeleton/*.py   # syntax
# engine unit test (no a2a-sdk needed): import agent_skeleton.tools and call the fns
```

Because `executor.py`/`serve.py` degrade gracefully when `a2a-sdk` is missing
(the base class becomes `object`), you can import and test the engine and the
alignment check without installing the SDK; only *serving* requires it.

## 8. When extending the protocol layer

This template intentionally mirrors the disaster agent's A2A usage. If you fix a
protocol bug here, note that the parent repo has **~5 divergent copies** of the
A2A/directory stack (see [`../CLAUDE.md`](../CLAUDE.md) §8) — a fix here does not
propagate to them.
