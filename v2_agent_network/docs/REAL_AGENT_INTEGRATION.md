# Real Agent Integration

Milestone 4A keeps the mock agents as contract fixtures and documents how a real
team can replace one with an external A2A server.

## What This Repo Expects

A real agent must provide:

- an A2A Agent Card with name, organization, version, skills, input/output modes,
  endpoint URL, transport, auth requirements, risk level, read/write
  classification, and examples;
- an A2A HTTP endpoint compatible with the installed `a2a-sdk==0.3.2`;
- task results that preserve `context_id`, `task_id`, message parts, task state,
  artifacts, and structured output when available;
- file or image references for large payloads instead of repeated inline bytes.

## Agent Card And OASF / ADS Records

Agent Cards describe how to interact with an A2A server. OASF / ADS records
describe how the agent is published and discovered. The converter keeps the
Agent Card under the record's interaction metadata so PI can discover through
Directory and then invoke through A2A.

Demo skill names such as `weather/forecast`, `calendar/search_availability`,
`email/draft`, `image/geospatial_analysis`, `intelligence/situation_summary`,
and `code/repository_inspection` are custom demo skill tags unless an official
OASF taxonomy mapping is present locally. Do not present those custom tags as
official OASF skills.

## Required A2A Behavior

The endpoint must accept a real `a2a.types.SendMessageRequest` through the SDK's
JSON-RPC HTTP path and return an SDK-compatible task or message response. A
successful response should include a completed or meaningful intermediate task
state, task linkage, at least one agent message, and either artifacts or
structured output.

## Publishing

External agents are independent A2A servers. The PI Agent is also an HTTP
server when run with Uvicorn. `DirectoryClient` is a client object used by PI
and CLI scripts; it is not a persistent registry server.

Fake ADS is in-memory and per-process. Running `publish_agents.py` in one
process does not populate a separately running PI server. Load cards into the PI
process with `PI_AGENT_CARD_PATHS` or `run_pi_agent_server.py --card` only for
fake ADS mode. Use real ADS for shared discovery between separately running
processes.

```text
User/API
  -> PI Agent server
  -> DirectoryClient
  -> ADS or fake ADS
  -> A2AClient
  -> external Email Agent A2A server
```

Runtime modes:

1. Direct A2A endpoint validation: validate a known endpoint with
   `validate_agent_endpoint.py --endpoint`. This mode does not perform
   discovery and publishes only when `--publish` is supplied.
2. Real ADS local discovery: start `dirctl daemon`, publish records through
   `ADS_BACKEND=cli`, search ADS, and let PI discover from ADS.
3. Fake ADS: process-local test backend only. It is not shared between CLI and
   PI server processes.

Dry-run local card validation:

```bash
AGENT_NETWORK_FAKE_ADS=false v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents --dry-run
```

Publish sample records to fake ADS:

```bash
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents
```

Start local ADS and publish the live Email Agent record to real ADS:

```bash
# install dirctl first if `command -v dirctl` is empty
dirctl daemon start
AGENT_NETWORK_FAKE_ADS=false \
ADS_BACKEND=cli \
ADS_SERVER_ADDR=localhost:8888 \
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json
```

The real CLI publish path is verified. It does not report success after only
submitting `routing publish`; it polls `routing list --cid <cid>` and succeeds
only once ADS has materialized the CID into the routing index.

Search routed ADS entries using the Email Agent's official routing hint:

```bash
dirctl --server-addr localhost:8888 routing search \
  --skill natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search \
  --output json
```

Plain `dirctl search` reads stored records and can include stale or orphaned
records. Treat `routing search` / `routing list` as the live discovery surface.

`ADS_BACKEND=cli` is the default real ADS backend and does not import the Python
`agntcy-dir` SDK. `ADS_BACKEND=sdk` is optional and may require an isolated
protobuf-compatible environment.

## ADS Host Operations

To expose a local ADS daemon on the LAN, prefer the managed launcher:

```bash
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.manage_ads_lan start \
  --listen-host 10.229.117.37 \
  --listen-port 8888
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.manage_ads_lan status
```

The launcher starts `dirctl daemon start` if needed, proxies
`10.229.117.37:8888` to `127.0.0.1:8888`, writes a PID file, and logs remote
connections.

Use the diagnostic command when a record appears in storage but not in routed
discovery:

```bash
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.ads_diagnostics \
  --server-addr localhost:8888
```

CID states:

- `healthy`: stored and present in routing.
- `queued`: `routing publish` was submitted but has not been indexed.
- `orphaned`: stored with no publication row.
- `stale`: stored but currently unrouted after a prior publish/unpublish state.

The host operator can re-submit routing for a selected CID:

```bash
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.repair_ads_routing \
  --server-addr localhost:8888 \
  --cid <cid>
```

Use `--restart-daemon-if-stuck` only as an explicit repair step. It is disabled
by default so daemon restarts are never hidden behavior.

## Endpoint Validation

Local mock validation without a running HTTP server:

```bash
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/weather_agent.card.json \
  --skill weather/forecast \
  --prompt "What is the weather near Austin?" \
  --mock-agent weather
```

External endpoint validation:

```bash
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card path/to/real_agent.card.json \
  --skill weather/forecast \
  --prompt "What is the weather near Austin?" \
  --endpoint http://host:port/
```

The default validation path does not publish to ADS. Add `--publish` only when
the endpoint should also be registered through Directory. Use `--fake-ads` with
`--publish` for local fake publication.

Safe live Email Agent validation commands:

```bash
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json \
  --skill email/current_time \
  --prompt "What time is it for the email agent?" \
  --endpoint http://10.229.117.223:9103/
```

```bash
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json \
  --skill email/draft \
  --prompt "Draft a short test email to myself, but do not create a Gmail draft and do not send it." \
  --endpoint http://10.229.117.223:9103/
```

For `email/draft`, the validator sends `create_gmail_draft=false` metadata.
Automated validation must not invoke `email/send`, create calendar events,
create docs, write sheets, or mutate Google Workspace state.

`validate_agent_endpoint.py` validates a known Agent Card and endpoint. It does
not perform discovery unless `--publish` is supplied.

## PI Server Card Loading

For a separately running PI server with fake ADS, load external Agent Cards into
the PI process itself:

```bash
AGENT_NETWORK_FAKE_ADS=true \
PI_AGENT_CARD_PATHS=v2_agent_network/configs/agent_cards/email_agent.card.json \
uvicorn v2_agent_network.services.pi_agent.main:create_app --factory --host 0.0.0.0 --port 8080
```

Alternatively:

```bash
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_pi_agent_server \
  --fake-ads \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json
```

For real ADS, publish the card to ADS instead of using `PI_AGENT_CARD_PATHS`.
Then run PI with `AGENT_NETWORK_FAKE_ADS=false`, `ADS_BACKEND=cli`, and
`ADS_SERVER_ADDR=localhost:8888`. The real ADS Email demo exercises this path
in-process:

```bash
AGENT_NETWORK_FAKE_ADS=false \
ADS_BACKEND=cli \
ADS_SERVER_ADDR=localhost:8888 \
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_real_ads_email_discovery_demo \
  --skill email/current_time \
  --prompt "What time is it for the email agent?"
```

## LLM-Powered PI Tool Loop

PI can optionally use OpenAI as a tool-loop agent over discovered A2A agents:

```text
User text
  -> PI LLM tool loop
  -> discover_agents through DirectoryClient / ADS
  -> inspect_agent for safe Agent Card skill metadata
  -> call_agent through policy.py and A2AClient
  -> final_answer, ask_user, or request_confirmation
  -> external agent endpoint
```

The LLM is not a registry or endpoint selector. It must not output endpoint
URLs, credentials, or raw HTTP destinations. It requests protected Python tools,
and PI enforces DirectoryClient discovery, policy decisions, confirmation state,
and A2A invocation.

Enable it on the PI server:

```bash
OPENAI_API_KEY=<your-key> \
PI_LLM_ENABLED=true \
PI_LLM_PROVIDER=openai \
PI_LLM_MODEL=gpt-5.5 \
PI_LLM_REASONING_EFFORT=low \
PI_LLM_VERBOSITY=low \
PI_AGENT_MODE=tool_loop \
AGENT_NETWORK_FAKE_ADS=false \
ADS_BACKEND=cli \
ADS_SERVER_ADDR=localhost:8888 \
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_pi_agent_server \
  --host 0.0.0.0 \
  --port 8080
```

If `OPENAI_API_KEY` is missing while `PI_LLM_ENABLED=true`, PI fails with a
configuration error. If `PI_LLM_ENABLED=false`, PI keeps the deterministic
router behavior. Set `PI_AGENT_MODE=legacy` to use the older one-shot workflow
planner for debugging.

Policy remains the authority for side effects. If the LLM plans `email/send`,
`email/calendar/create_event`, Google Docs or Sheets writes, or repository
modification skills, PI returns `confirmation_required` and does not invoke A2A
automatically.

## Agent Checklists

Weather agents should expose `weather/current_conditions`, `weather/forecast`,
`weather/alerts`, and `weather/route_weather_assessment` as applicable.

Calendar mutations such as create, update, and delete must require confirmation.
Email `send` must require confirmation. Coding actions that run commands, write
files, create commits, or apply patches must require confirmation.

The Gmail-backed Email Agent can perform real Gmail, Calendar, Docs, and Sheets
actions. `email/send`, `email/calendar/create_event`, `email/docs/create`,
`email/docs/append`, `email/docs/replace_text`,
`email/docs/create_and_email`, `email/sheets/create`, `email/sheets/write`, and
`email/sheets/append` must require confirmation. `email/draft` is read-only only
when `create_gmail_draft=false`; creating a Gmail draft is a write action.

Photo/Geospatial agents should accept file or artifact references. Intelligence
agents should accept prior structured outputs or artifact references and return
source artifact references in their result.

## Troubleshooting

- If validation fails before sending a task, check Agent Card URL, skill names,
  and input/output modes.
- If mock validation fails, the contract fixture has regressed.
- If real ADS CLI mode fails with `dirctl is not installed`, install the
  `dirctl` binary or use the published container image. If it fails with a
  connection error, run `dirctl daemon start` and set
  `ADS_SERVER_ADDR=localhost:8888`.
- If `dirctl search` finds a CID but `dirctl routing list` does not, the record
  is stored but not live-discoverable. Run `ads_diagnostics.py`; then repair or
  re-publish until `routing list --cid <cid>` shows the CID.
- If `ADS_BACKEND=sdk` fails with `DirectoryUnavailable`, inspect the included
  `protobuf` and `agntcy-dir` versions and use an isolated compatible SDK
  environment. CLI mode does not require the Python SDK.
- If the PI cannot discover an agent, publish the card/record first and confirm
  the requested skill is present as an official OASF skill or custom skill tag.
