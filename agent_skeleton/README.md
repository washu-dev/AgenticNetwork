# Agent runtime (`agent_skeleton`)

`agent_skeleton` is the **agent runtime**: the engine every hosted agent in
**AgenticNetwork** (the "Internet of Agents") imports at run time. The
registration service installs it into each agent's environment and copies it into
each built agent image. It holds:

- the **LLM tool loop** ([`llm_loop.py`](llm_loop.py)) and the **`AgentSpec`**
  ([`spec.py`](spec.py)) that tells the loop which prompt and tools one agent uses;
- the **A2A executor and server** ([`executor.py`](executor.py),
  [`serve.py`](serve.py), `a2a_runtime.py`);
- **`AgentHandler`** ([`base.py`](base.py)), the base class for custom agents, and
  `HandlerExecutor` ([`handler_executor.py`](handler_executor.py)), which serves
  one over A2A;
- **`container_runner.py`**, the entry point of every built agent image;
- the **system tools** agents can be given ([`system_tools/`](system_tools/):
  `call_endpoint` and the presets `calculator` and `current_time`).

It also holds two smaller things: the **wrapper-repo generator**
([`generator/`](generator/)) and one **demo agent** ([`demo/`](demo/)): a card, a
system prompt and two text tools.

The engine was distilled from the reference agent `disaster_response_agent.py`
(now in the separate [`agent-directory`](https://github.com/washu-dev/agent-directory)
repo, with the other specialist agents) and split into small, labeled files.

> **Anything under [`demo/`](demo/) is safe to change; anything else in
> `agent_skeleton/` is runtime that reaches every hosted agent.** Nothing in the
> runtime imports the demo, and the demo is not installed, vendored into images or
> copied by the generator. For example, `normalize_result()` in `prompt.py` shapes
> the result of every hosted LLM agent that does not bring its own. Don't build an
> agent by editing the runtime: write one of the three things below.

---

## Building an agent: pick one of three

You register the agent through the registration service. See
[`../docs/BUILDING_AGENTS.md`](../docs/BUILDING_AGENTS.md) for the walkthrough and
[`../docs/REGISTRATION_SERVICE.md`](../docs/REGISTRATION_SERVICE.md) ("Handler types")
for the exact settings.

| You want | Register as | You write |
|---|---|---|
| Your own Python in charge of each request | `custom` | A class that subclasses `AgentHandler` and implements `handle_structured()`. You upload it; an admin approves it. |
| An LLM that calls your own Python tools | `llm` with `tools.source = "upload"` | One tools module with `TOOL_SCHEMAS` and `TOOL_REGISTRY` (or a `build_tools(config)` function that returns both). You upload it; an admin approves it. |
| No code at all | `proxy`, or `llm` with `tools.source = "http"`, `"presets"` or `"none"` | A form in the registration UI. A `proxy` forwards each request to your service with no LLM; an `http` agent gives the LLM one tool per operation of your API. |

A tools module has the same shape as the demo's [`demo/tools.py`](demo/tools.py),
which can be uploaded as is.

A custom handler looks like this:

```python
from agent_skeleton import AgentHandler

class MyHandler(AgentHandler):
    async def handle_structured(self, user_input, files=None, context=None):
        return {"answer": "..."}   # "answer" (a string) is required; other keys are structured output
```

`handle_structured` may also be a plain `def`: the executor runs a sync handler in
a worker thread, so blocking code doesn't stall the server. A sync handler that
overruns `max_runtime_seconds` is reported failed but cannot be stopped, so
long blocking work should check its own deadline.

### The name `agent_skeleton` is reserved

The import name is a public interface. Uploaded handlers import it, and every
built agent image starts with `python -m agent_skeleton.container_runner`. The
platform supplies the real package, so an upload must not bring its own:

- no top-level `agent_skeleton/` folder (or `Dockerfile`) in the upload, and
- no `pyproject.toml` that installs a package by that name (`name = "agent-skeleton"`,
  or an `agent_skeleton` entry under `package-dir` or `packages`).

Such a package would replace the platform's copy when the image is built, the
entry point would disappear, and the agent would not start.
`registration_service/archive.py` rejects these uploads. Name your project after
your agent and package only your own code.

An agent run locally by the registration service has the uploaded project first on
its import path, and it imports `agent_skeleton`, `agentnet` (the platform's shared
package) and `registration_service` from the repository. A top-level module or
package with one of those names would replace the platform's, so the upload may not
have one: no folder (`agentnet/`), module file (`agentnet.py`, a compiled
`agentnet.*.so`) or `entry_file` by that name. Any other top-level name shadows an
installed package of the same name the same way (a `pydantic.py` replaces
pydantic), so name your modules after your agent.

---

## The 10-second mental model

An agent is a **small HTTP service** that does two things:

1. **Advertises skills** via a JSON **card**, which is *published* into the
   directory (ADS) so the planner can discover it.
2. Answers requests through **one `execute()` method**, which the runtime's
   executor provides. The SDK turns an inbound A2A request into a call to it; the
   executor runs the agent and turns its result into the response. **Agent code
   never parses the wire protocol.**

That's it. The planner (the LLM "brain") finds agents by skill and delegates whole
tasks to them. Adding an agent needs **zero planner code changes**.

Agents don't call other agents through the directory today; only the planner does.
(`call_endpoint` can call the one fixed A2A endpoint an agent wraps. Tools such as
`call_agent` are planned; see `system_tools/__init__.py`.)

---

## File map

| File | Part | What it is |
|---|---|---|
| [`llm_loop.py`](llm_loop.py) | engine | The generic LLM tool-calling loop (`run_tool_loop`, `run_agent`) |
| [`spec.py`](spec.py) | engine | `AgentSpec` (prompt + tools as data) and the spec builders for each agent kind |
| [`executor.py`](executor.py) | engine | `SkeletonAgentExecutor`: `execute()` + A2A I/O helpers |
| [`serve.py`](serve.py) | engine | `create_app` (a spec → A2A web app) + the `serve-wrapper` CLI |
| `a2a_runtime.py` | engine | `a2a-sdk` import guard + Part/TaskUpdater wrappers |
| [`base.py`](base.py), [`handler_executor.py`](handler_executor.py) | engine | `AgentHandler` + `FileInput`; `HandlerExecutor` (heartbeat, runtime cap, credential context) |
| `container_runner.py` | engine | Entry point of every built agent image: a custom handler, or an LLM loop over uploaded tools |
| [`system_tools/`](system_tools/) | engine | `call_endpoint` and the preset tools |
| `config.py` | engine | Defaults for every agent (step limit, heartbeat name, host/port) + env-var readers |
| [`prompt.py`](prompt.py) | engine | `normalize_result()` and the wrapper prompt builders |
| [`tools.py`](tools.py) | engine | `validate_tool_registry()`, the schema ↔ function check |
| [`generator/`](generator/) | tooling | `answers.json` → a deployable wrapper-agent folder |
| `pyproject.toml` | packaging | Installs the package as `agent-skeleton` (without `demo/`) |
| [`demo/`](demo/) | **demo (safe to edit)** | `SYSTEM_PROMPT` + `demo_spec()` (`__init__.py`), the tools (`tools.py`), the card (`agent.card.json`, "Skeleton Agent"), the `check` / `serve` commands (`__main__.py`) |

The runtime has no default agent: `run_agent`, `SkeletonAgentExecutor` and
`create_app` all require a spec.

---

## Run the demo agent

The demo is not part of a regular install, so run it from the repo root (or with
the editable install below).

```bash
# 1. Install deps (the repo's dev venv, .venv from requirements-dev.txt, already has them), or:
pip install -e ./agent_skeleton      # from the repo root: a2a-sdk[http-server]==0.3.2, openai, uvicorn

# 2. Point at an LLM (hosted OpenAI, or a self-hosted vLLM endpoint)
export OPENAI_API_KEY=sk-...            # any non-empty placeholder works for vLLM
# export OPENAI_BASE_URL=http://<vllm-host>:8100/v1   # only for vLLM
export AGENT_MODEL=gpt-4o-mini          # or your --served-model-name

# 3. Sanity-check that the demo's schemas and functions agree (no LLM needed)
python -m agent_skeleton.demo check
# -> OK: tool schemas and functions are aligned.

# 4. Serve it (from the repo root)
python -m agent_skeleton.demo serve
# -> Serving Skeleton Agent on 0.0.0.0:9110 (url=http://127.0.0.1:9110/)
```

To make it **discoverable** by the planner, publish the card to the directory
(needs the `dirctl` binary and a running ADS daemon). Agents registered through
the registration service are published by its supervisor instead.

```bash
python -m agentnet.scripts.publish_agents --card agent_skeleton/demo/agent.card.json
```

---

## Running the engine by hand: endpoint-wrapper mode + the generator

The registration service is the usual way in, but the engine can also run outside it:

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
  complete, deployable agent folder (card + server + Dockerfile + entrypoint + a copy
  of this engine as `wrapper_engine/` + README) that self-publishes to ADS — stamping
  an `oasf:<name>:<id>` routing tag so it's discoverable:
  ```bash
  python -m agent_skeleton.generator \
    --answers agent_skeleton/generator/examples/weather_bridge.answers.json \
    --dest ../agent-directory
  ```

- **Spec builders.** Each kind of agent is one builder in `spec.py`:
  `llm_wrapper_spec` (preset tools from `system_tools/preset_tools.py`),
  `tool_loop_spec` (uploaded tools, loaded by `load_tool_module`),
  `operation_wrapper_spec` (one tool per API operation), `endpoint_wrapper_spec`
  and `multi_endpoint_wrapper_spec` (one or several wrapped endpoints).

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
  execute(context, event_queue)  ← the runtime's executor (SkeletonAgentExecutor
                                   runs a spec; HandlerExecutor calls a custom
                                   agent's handle_structured()).
```

- **uvicorn** is just the web-server process; it listens on the port and feeds
  HTTP into the app. Nothing A2A-specific.
- **`A2AStarletteApplication`** is the `a2a-sdk` class that *builds a Starlette
  (ASGI) web app for you*. You give it the card + a handler, call `.build()`,
  and hand the result to uvicorn.
- **`DefaultRequestHandler`** is the protocol engine: on an inbound request it
  constructs the `RequestContext` (the message, `task_id`, `context_id`,
  metadata) and an `EventQueue`, then `await`s the executor's `execute()`.

**Nothing parses raw A2A JSON by hand.** The executor calls
`context.get_user_input()` for text, reads `context.metadata` / DataParts for
structured input, and replies through a `TaskUpdater`. The wire format is entirely
the SDK's job.

What the model sees: the stock [`executor.py`](executor.py) merges request, message and
part metadata and DataPart data into the JSON user message, minus the credential keys
(`credentials`, `authorization`, `secret(s)`, `token`) and, from metadata, the user's
identity (`user_id`, `auth_user_id`, `mail`, `eppn`, `remote_user`, `remote-user`). A tool
cannot read the user's identity from the prompt; an `AgentHandler` gets `user_id` in its
`context` argument.

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
4. For each call: `json.loads(arguments)` → dispatch to the spec's Python function →
   append a `{"role": "tool", "tool_call_id", "content"}` message.
5. Re-call the model with the grown history. Repeat (capped at `MAX_TOOL_STEPS`);
   the call after the last round sends `tool_choice="none"`, so the model answers
   in text, and if it still gives none the answer says the step limit was reached.

**Why resend the whole history?** vLLM / Chat Completions is *stateless* — no
server-side memory. The agent keeps the conversation locally and resends it each
step. The required order is `system → user → assistant(with tool_calls) →
tool-result(s) → …`, which is exactly what step 3-before-4 preserves.

### The response — dual channel

On every terminal path the executor emits **both**:
- a structured **`DataPart` artifact** (machine-readable — what the planner reads),
- a **text message** (human-readable), with the same dict echoed under
  `metadata.structured_output`.

See `_complete` / `_failed` / `_requires_input` in [`executor.py`](executor.py).

### The schema ↔ function alignment check (the improvement)

The specialist agents have **no** check that a tool's JSON schema matches its Python
function — a mismatch just fails silently at runtime. The runtime adds
`validate_tool_registry()` in [`tools.py`](tools.py). The spec builders in `spec.py`
run it before they return (`AgentSpec.validate()`), `create_app` runs it at startup,
and `python -m agent_skeleton.demo check` runs it on the demo. It verifies the
envelope (`"type": "function"`, `parameters.type` `"object"`), that every schema has
a function, every property is a parameter, optional properties have defaults, and
there are no undeclared required parameters. See *CLAUDE.md → "Closing the gap further"*
for the even-stronger option (generate schemas *from* typed functions).

---

## Reference

The engine files map back to the reference implementation, `disaster_response_agent.py`
— its tool schemas, `_call_model` loop, `_run_tool` dispatch, `execute` executor,
and `create_disaster_a2a_app` serve. That agent now lives in
the separate [`agent-directory`](https://github.com/washu-dev/agent-directory) repo
(the specialist agents were extracted there). Background contracts:
[`../docs/deployment/AGENT_INTEGRATION_SPEC.md`](../docs/deployment/AGENT_INTEGRATION_SPEC.md)
and [`../CLAUDE.md`](../CLAUDE.md).
