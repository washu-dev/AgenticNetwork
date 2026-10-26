# Building Agents

How to add a new agent to the network. There are three ways in, from most to least hand-work:
write a standalone agent, upload a custom handler, or generate an endpoint-wrapper. All of them
end the same way: an Agent Card published to ADS, reachable over A2A. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the contracts and [REGISTRATION_SERVICE.md](REGISTRATION_SERVICE.md)
for the registration/approval flow.

## The one contract every agent satisfies

1. **An Agent Card** (`*.card.json`): `name`, `url` (where it listens), `version`, and a `skills[]`
   list — each skill has a slash-namespaced `id` (e.g. `myagent/do_thing`), a `description`, and
   `examples`. Tag side-effecting skills with `write`.
2. **An executor** — a class extending the `a2a-sdk` `AgentExecutor` with
   `execute(context, event_queue)`: read `context.get_user_input()`, do the work, return a **text
   summary + a structured-data `DataPart` artifact** (the machine-readable channel the planner
   reads).
3. **Served** over A2A (`A2AStarletteApplication` + `DefaultRequestHandler`, uvicorn on a port).
4. **Published** to ADS on startup (convert card → OASF record, `dirctl push` + `routing publish`,
   poll routing), unpublished on shutdown.

Conventions: slash-namespaced skill ids; structured output as a `DataPart`; if work is slow,
advertise `capabilities.streaming: true` and emit progress (the coding agent uses a ~25s heartbeat
to beat the caller's read timeout).

## `agent_skeleton/` — the shared engine

The skeleton provides the reusable machinery. It has **two executor lanes** for two different kinds
of agent — this split is the same split as the two credential-sourcing models ([AUTH.md](AUTH.md)
§4, [DECISIONS.md](DECISIONS.md) D3/D6):

| Lane | Executor | For | Credential model |
|---|---|---|---|
| Uploaded code | `HandlerExecutor` (`handler_executor.py`) | a user-written `AgentHandler` run as a custom agent | **per-user** (injected `context`) |
| Endpoint-wrapper | `SkeletonAgentExecutor` (`executor.py`) via `spec.py` | wrapping an external HTTP/A2A API with no code | **operator env-var** |

They are independent classes wired by different paths — the uploaded lane via
`registration_service/handlers/custom_handler.py` + `container_runner.py`; the wrapper lane via
`serve.py::create_app` (the `serve-wrapper` CLI).

## Lane 1 — uploaded custom handler (per-user credentials)

Write a small handler and upload it (see [REGISTRATION_SERVICE.md](REGISTRATION_SERVICE.md) for the
upload/approve flow):

```python
from agent_skeleton import AgentHandler

class MyHandler(AgentHandler):
    async def handle_structured(self, user_input, files=None, context=None):
        # context (optional 3rd param) carries {"credentials": {...}, "user_id": ...}
        creds = (context or {}).get("credentials") or {}
        api_key = (creds.get("openai_api_key") or {}).get("api_key")
        ...
        return {"answer": "...", "data": {...}}   # "answer" (a string) is required; other keys are structured output
```

`handle_structured` may also be a plain `def`: it then runs in a worker thread, and if it overruns
`max_runtime_seconds` it is reported failed but not stopped (see
[agent_skeleton/README.md](../agent_skeleton/README.md)).

Upload a single `.py`, or a `.zip` whose root holds the entry file next to the packages it imports.
Zip only the project's own files, for example `zip -r agent.zip handler.py mypkg requirements.txt`.
The platform's own package names are reserved: no top-level module, package or entry file may be
named `agent_skeleton`, `agentnet` or `registration_service` (nor a `Dockerfile` or `*.card.json`),
since locally run agents have the project first on their import path (see
[agent_skeleton/README.md](../agent_skeleton/README.md), "The name `agent_skeleton` is reserved").
Every file in the upload is stored, shown to the approver and, on the cluster and Cloud Run,
committed to the agent-directory repository and built into the agent's image, which is public on
Docker Hub: never include `.env` files or other secrets. Those two backends also take text files
only, so a stray binary such as `.DS_Store` makes the approval fail. Files under `.git/`, `.venv/`
and `__pycache__/` are skipped (the upload response says how many), so the approver reviews exactly
the code that runs. They still count toward the archive's file limit (2,000), which a virtualenv
alone usually exceeds (and one named `venv/` or `env/` is not skipped at all).

**The credential seam (P4d):** `HandlerExecutor.execute` inspects your `handle_structured`
signature — if it accepts a 3rd `context` param (or `**kwargs`), the executor extracts credentials
from the inbound A2A request metadata and passes `context = {"credentials", "user_id"}`. A 2-arg
handler (`handle_structured(self, user_input, files=None)`) still works unchanged. The `context` is
never logged. To *receive* a per-user credential, declare it at registration (a
`CredentialRequirement` with the `env_var`/`type` you read it under) — the user then grants it and
the planner scopes + injects it. See [AUTH.md](AUTH.md) §4.

Trust boundary: uploaded code isn't sandboxed by the validator — it runs only after **admin
approval** and inside per-agent **container isolation** ([DECISIONS.md](DECISIONS.md) D2).

## Lane 2 — endpoint-wrapper (operator credential, often no code)

For wrapping an existing HTTP/JSON (or A2A) API. `system_tools/call_endpoint.py` calls the upstream
from inside the LLM loop; auth is read at call time from an **env var named on the card**
(`EndpointConfig.auth_env`) — the token itself is never stored, and secret-shaped strings are
redacted from error bodies. `spec.py`'s `AgentSpec` selects the prompt + tools so one frozen engine
serves any config via `AGENT_ENDPOINT_*` env vars.

**No code, through the registration UI:** register an `llm` agent with `tools.source = "http"`
(one typed tool per operation of an API, `spec.operation_wrapper_spec`) or `"presets"` (the
built-in catalog in `system_tools/preset_tools.py`); the service runs it on this same engine
([DECISIONS.md](DECISIONS.md) D13). A `proxy` agent forwards each call to an upstream service with
no LLM.

The **generator** (`generator/`) turns an `answers.json` (see
`generator/examples/weather_bridge.answers.json`) into a complete, deployable wrapper-agent folder,
stamping an `oasf:<name>:<id>` routing tag on the card so it's discoverable without editing ADS
maps (see [AGENT_DIRECTORY.md](AGENT_DIRECTORY.md)).

> This lane serves an *operator-owned* credential (one shared token for the wrapped API). If a
> wrapper ever needs *per-user* auth, that's the deferred reconciliation in
> [DECISIONS.md](DECISIONS.md) D6 — the wrapper lane doesn't inject per-user credentials today.

## Lane 3 — standalone agent (full control)

Copy the simplest template (`disaster_response_agent/`) or the cleanest modern one
(`scout_agent/`), implement your own `AgentExecutor`, serve it, and publish on startup (copy a
`publish_*_to_ads` helper). This is what the specialist agents in the `agent-directory` repo do;
"The one contract" above is the checklist.

## After it's running
The planner discovers it automatically on its next `list_agents` — **no planner change needed**.
Declarations (skills, credentials) only propagate on (re)publish, so changing them requires a
re-publish. Validate an endpoint without publishing:
```bash
python -m agentnet.scripts.validate_agent_endpoint \
  --card <card.json> --skill <skill> --prompt "..." --endpoint http://host:port/
```

## Key files
| Path | Role |
|---|---|
| `agent_skeleton/base.py` | `AgentHandler` ABC (the `handle_structured` contract + optional `context`) |
| `agent_skeleton/handler_executor.py` | `HandlerExecutor` — uploaded-code lane + per-user credential seam |
| `agent_skeleton/container_runner.py` | ENTRYPOINT of every built per-agent image (ENGR cluster and Cloud Run) → `HandlerExecutor`, or the tool loop for `llm` + uploaded tools |
| `agent_skeleton/executor.py` / `spec.py` | `SkeletonAgentExecutor` + `AgentSpec` — endpoint-wrapper lane |
| `agent_skeleton/system_tools/call_endpoint.py` | external-endpoint tool (operator env-var auth) |
| `agent_skeleton/system_tools/preset_tools.py` | built-in preset-tool catalog for registration `llm` agents with `tools.source = "presets"` |
| `agent_skeleton/serve.py` | `create_app`, `serve-wrapper` CLI |
| `agent_skeleton/generator/` | `answers.json` → deployable wrapper-agent folder |
| `agent_skeleton/demo/` | the demo agent (safe to edit; not installed or vendored); its `tools.py` has the uploaded-tools shape |
| `registration_service/handlers/custom_handler.py` | wires an uploaded handler into `HandlerExecutor` |
