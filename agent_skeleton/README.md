# Agent Skeleton

A minimal, **copy-to-start template** for building an agent that plugs into
**AgenticNetwork** (the "Internet of Agents"). It is a faithful, trimmed-down
distillation of the reference agent `disaster_response_agent.py` (~2,456 lines;
now in the separate [`agent-directory`](https://github.com/washu-dev/agent-directory)
repo alongside the other specialist agents) split into small, labeled
files — so you can see, at a glance, the few pieces you actually write versus the
plumbing you just keep.

> **TL;DR for building an agent:** edit four things — `agent.card.json`,
> [`tool_schemas.py`](tool_schemas.py), [`tools.py`](tools.py),
> [`prompt.py`](prompt.py) — and run `python -m agent_skeleton.serve serve-a2a`.
> The rest is boilerplate that's identical across every agent in the repo.

---

## The 10-second mental model

An agent is a **small HTTP service** that does three things:

1. **Advertises skills** via a JSON **card** — and *publishes* that card into the
   directory (ADS) so the planner can discover it.
2. Implements **one `execute()` method** — the SDK turns an inbound A2A request
   into a call to it, hands you the input, and turns what you emit into the
   response. **You never parse the wire protocol.**
3. **(Optional)** Calls *other* agents over the same A2A protocol, as tools.

That's it. The planner (the LLM "brain") finds you by skill and delegates whole
tasks to you. Adding your agent needs **zero planner code changes**.

---

## File map — the six zones + support

| Zone | File | What it is | You… |
|---|---|---|---|
| 1 | [`tool_schemas.py`](tool_schemas.py) | Your tools in Chat Completions JSON shape | **WRITE** |
| 2 | [`prompt.py`](prompt.py) | System prompt + result normalization | **WRITE** |
| 3 | [`llm_loop.py`](llm_loop.py) | The generic LLM tool-calling loop | copy |
| 4 | [`tools.py`](tools.py) | Tool functions + `{name: fn}` registry + alignment check | **WRITE** bodies |
| 5 | [`executor.py`](executor.py) | The A2A executor (`execute()` + I/O helpers) | copy |
| 6 | [`serve.py`](serve.py) | Serve (uvicorn) + ADS publish/unpublish + CLI | copy |
| — | [`agent.card.json`](agent.card.json) | Your skills & endpoint | **WRITE** |
| — | `config.py` | Constants & env vars | edit defaults |
| — | `a2a_runtime.py` | `a2a-sdk` import guard + Part/TaskUpdater wrappers | copy |

The four bolded rows are your agent. The rest is the same in every agent in this
repo (which is *why* the originals are thousands of lines — they each re-ship the
plumbing).

---

## Quickstart

```bash
# 1. Install deps into a venv/conda env (NOT another component's env)
pip install a2a-sdk openai uvicorn

# 2. Point at an LLM (hosted OpenAI, or a self-hosted vLLM endpoint)
export OPENAI_API_KEY=sk-...            # any non-empty placeholder works for vLLM
# export OPENAI_BASE_URL=http://<vllm-host>:8100/v1   # only for vLLM
export AGENT_MODEL=gpt-4o-mini          # or your --served-model-name

# 3. Sanity-check that your schemas and functions agree (no LLM needed)
python -m agent_skeleton.serve check
# -> OK: tool schemas and functions are aligned.

# 4. Serve it (from the repo root)
python -m agent_skeleton.serve serve-a2a
# -> Serving Skeleton Agent on 0.0.0.0:9110 (url=http://127.0.0.1:9110/)
```

To make it **discoverable** by the planner, publish the card to the directory
(needs the `dirctl` binary and a running ADS daemon):

```bash
python -m agent_skeleton.serve serve-a2a --publish-ads --ads-url 127.0.0.1:8888
# (recommended alternative for production-grade OASF conversion:)
python -m agent_directory_service.scripts.publish_agents --card agent_skeleton/agent.card.json
```

---

## Two more ways to use this: endpoint-wrapper mode + the generator

Beyond the copy-to-start template above, `agent_skeleton` now also provides a
**spec-driven engine** so you can stand up agents *without writing tool code*:

- **Endpoint-wrapper mode (no code).** Wrap an external HTTP/JSON (or A2A) endpoint
  in an LLM loop — the model decides *what* to send; a config fixes *where/how/auth*.
  The only tool is `call_endpoint` (`system_tools/call_endpoint.py`: env-var auth read
  at call time, secret redaction, 2 MB response cap, no-redirect). Supply a card +
  endpoint env vars:
  ```bash
  AGENT_ENDPOINT_URL=https://api.example.com/run \
  AGENT_ENDPOINT_AUTH_ENV=EXAMPLE_TOKEN \
  python -m agent_skeleton.serve serve-wrapper --card my.card.json
  ```
  Under the hood `spec.py`'s `AgentSpec` selects prompt+tools as *data*, so one frozen
  `run_tool_loop` serves any configuration (`run_agent(spec=...)`).

- **Generator (`generator/`).** Turn an `answers.json` describing an endpoint into a
  complete, deployable agent folder (card + server + Dockerfile + entrypoint + vendored
  `wrapper_engine/` + README) that self-publishes to ADS — stamping an `oasf:<name>:<id>`
  routing tag so it's discoverable:
  ```bash
  python -m agent_skeleton.generator \
    --answers agent_skeleton/generator/examples/weather_bridge.answers.json \
    --dest ../agent-directory
  ```
  (`agent-directory/weather-bridge-agent/` is a worked output.)

- **Uploaded-code path.** For arbitrary user code, subclass `AgentHandler` (`base.py`)
  and implement `handle_structured()`; `HandlerExecutor` / `container_runner.py` run it
  — this is the lane the registration service's `custom` handler uses.

---

## How to build *your* agent (the only steps that matter)

1. **Card** (`agent.card.json`): set `name`, `url`, `version`, and a `skills[]`
   list. Each skill has a slash-namespaced `id` (e.g. `myagent/do_thing`), a
   `description`, and `examples`. Tag side-effecting skills `"write"`.
2. **Tool schemas** (`tool_schemas.py`): one entry per tool, standard Chat
   Completions shape. List required vs optional params.
3. **Tool functions** (`tools.py`): write one Python function per tool, with
   keyword args named exactly like the schema's properties, returning a dict.
   Add each to `TOOL_REGISTRY`.
4. **Prompt** (`prompt.py`): write `SYSTEM_PROMPT` (behavior + output contract)
   and adjust `normalize_result()` to the keys you want to return.

Run `python -m agent_skeleton.serve check` — it will tell you immediately if a
schema and a function disagree. Then serve.

---

## Concepts (the demystified version)

### Request lifecycle — the layering

```
  HTTP on :9110
      │
  uvicorn ................. ASGI HTTP server. Owns the socket, speaks raw HTTP.
      │
  Starlette app ........... built by A2AStarletteApplication(card, handler).build()
      │                     HTTP routing + JSON-RPC / SSE framing.
  DefaultRequestHandler ... a2a-sdk protocol engine. Parses the JSON-RPC, builds
      │                     a RequestContext + EventQueue, tracks Task state.
  YOUR execute(context, event_queue)  ← the only code you write here.
```

- **uvicorn** is just the web-server process; it listens on the port and feeds
  HTTP into the app. Nothing A2A-specific.
- **`A2AStarletteApplication`** is the `a2a-sdk` class that *builds a Starlette
  (ASGI) web app for you*. You give it the card + a handler, call `.build()`,
  and hand the result to uvicorn.
- **`DefaultRequestHandler`** is the protocol engine: on an inbound request it
  constructs the `RequestContext` (the message, `task_id`, `context_id`,
  metadata) and an `EventQueue`, then `await`s your `execute()`.

**You never parse raw A2A JSON.** You call `context.get_user_input()` for text,
read `context.metadata` / DataParts for structured input, and reply through a
`TaskUpdater`. The wire format is entirely the SDK's job.

### The tool loop — why extraction is deterministic

The LLM never free-texts its tool calls. Because the request sends `tools=...`,
the model is *constrained* by the API to emit calls as **structured JSON** in
`response.choices[0].message.tool_calls` (`id` + `function.name` +
`function.arguments`). The agent reads a typed field — no regex on prose. That's
why [`llm_loop.py`](llm_loop.py) can extract calls so reliably.

The loop ([`llm_loop.py`](llm_loop.py), `run_tool_loop`):

1. Call the model with `messages` + `tools`.
2. Read `tool_calls`. None → the model's `content` is the final answer; stop.
3. Append the assistant message (**with** its `tool_calls`) to history.
4. For each call: `json.loads(arguments)` → dispatch to your Python function →
   append a `{"role": "tool", "tool_call_id", "content"}` message.
5. Re-call the model with the grown history. Repeat (capped at `MAX_TOOL_STEPS`).

**Why resend the whole history?** vLLM / Chat Completions is *stateless* — no
server-side memory. The agent keeps the conversation locally and resends it each
step. The required order is `system → user → assistant(with tool_calls) →
tool-result(s) → …`, which is exactly what step 3-before-4 preserves.

### The response — dual channel

On every terminal path your executor emits **both**:
- a structured **`DataPart` artifact** (machine-readable — what the planner reads),
- a **text message** (human-readable), with the same dict echoed under
  `metadata.structured_output`.

See `_complete` / `_failed` / `_requires_input` in [`executor.py`](executor.py).

### The schema ↔ function alignment check (the improvement)

The real agents have **no** check that a tool's JSON schema matches its Python
function — a mismatch just fails silently at runtime. This template adds
`validate_tool_registry()` in [`tools.py`](tools.py), run at startup by
`create_app` and via `serve.py check`. It verifies every schema has a function,
every property is a parameter, optional properties have defaults, and there are
no undeclared required parameters. See *CLAUDE.md → "Closing the gap further"*
for the even-stronger option (generate schemas *from* typed functions).

---

## What's genuinely "no-code" vs not

- **A coder is required** to write *new capabilities* — the tool function bodies
  in [`tools.py`](tools.py) are real Python.
- **A non-coder can plausibly** assemble a **card + schema list + prompt** and
  *wire existing* tool functions. If the capabilities they need already exist,
  standing up a new agent is close to no-code.

---

## Reference

Every zone maps back to the reference implementation, `disaster_response_agent.py`
— its tool schemas, `_call_model` loop, `_run_tool` dispatch, `execute` executor,
`create_disaster_a2a_app` serve, and ADS publish helpers. That agent now lives in
the separate [`agent-directory`](https://github.com/washu-dev/agent-directory) repo
(the specialist agents were extracted there). Background contracts:
[`../deployment/AGENT_INTEGRATION_SPEC.md`](../deployment/AGENT_INTEGRATION_SPEC.md)
and [`../CLAUDE.md`](../CLAUDE.md).
