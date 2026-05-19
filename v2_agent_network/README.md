# V2 Agent Network

This directory contains the new AGNTCY-based PI-agent network. Milestones 1-2
implement only the Weather path: fake Directory discovery, A2A SDK adapters,
HTTP A2A transport, a mock Weather A2A server, and a PI HTTP fallback endpoint.
Milestone 3A adds Calendar and Email mock A2A agents plus a deterministic
Calendar -> Email draft workflow.
Milestone 3B adds Photo/Geospatial and Intelligence mock A2A agents plus a
deterministic Photo/Geospatial -> Weather -> Intelligence summary workflow.
Milestone 3C adds a mock Coding A2A agent for read-only repository inspection
and confirmation-gated repository modification proposals.
Milestone 4A adds explicit fake/real Directory backends, endpoint validation,
and `requested_skill` capability-hint handling with a seventh-agent acceptance
test.
Milestone 4B adds safe integration paths for a live Gmail-backed Email/Calendar
/ Google Workspace A2A agent.
Milestone 4C adds a real local ADS workflow through `dirctl`: publish the live
Email Agent record, search ADS by skill, let PI discover from ADS, and invoke
the live A2A endpoint for safe read-only skills.
Milestone 5A adds an optional OpenAI-powered PI tool-loop runtime. The LLM
repeatedly chooses protected PI tools such as DirectoryClient discovery,
Agent Card inspection, policy-checked A2A invocation, confirmation requests,
and final answer. Endpoints, policy enforcement, and A2A transport remain
outside the model.

This is not the v1 image-classification demo.

## Local Python Environment

V2 owns its runtime now. Create the local venv from the repo root:

```bash
python3 -m venv v2_agent_network/.venv
v2_agent_network/.venv/bin/python -m pip install --upgrade pip
v2_agent_network/.venv/bin/python -m pip install -r v2_agent_network/requirements.txt
```

All commands below assume this interpreter:

```bash
v2_agent_network/.venv/bin/python
```

For conda-based setups, use:

```bash
conda env create -f v2_agent_network/environment.yml
conda activate v2-agent-network
```

## OASF Skill Handling

Agent Cards describe how to interact with A2A servers. OASF/ADS records describe
how agents are published and discovered. The converter keeps these concepts
separate.

The Weather demo skills such as `weather/forecast` are stored as custom
demo-specific skill tags because no local official OASF taxonomy entry is known
for them in this repository. The same applies to Milestone 3A demo skills such
as `calendar/search_availability`, `calendar/create_event`, `email/draft`, and
`email/send`, and Milestone 3B demo skills such as
`image/geospatial_analysis`, `image/object_detection`,
`intelligence/situation_summary`, and `intelligence/course_of_action`. The fake
DirectoryClient searches both official OASF skills and custom skill tags, but
it does not pretend custom tags are official OASF taxonomy entries. Milestone
3C coding skills such as `code/repository_inspection`, `code/bug_fix`,
`code/pr_review`, `code/test_generation`, and `code/documentation` are also
stored as demo-specific custom skill tags.

For real ADS publication, the Email Agent record uses official OASF routing
skills where known for Directory routing, while project-specific skills such as
`email/current_time` remain custom tags in metadata. PI searches ADS using the
requested custom skill plus the configured official routing hints, pulls the
matching records, then post-filters by custom skill tag before policy and A2A
invocation. Custom tags are not treated as official OASF taxonomy entries.

## Milestone 2 Demo

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_weather_demo
```

## Milestone 3A Calendar + Email Demo

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_calendar_email_demo
```

The demo prompt is:

```text
Check my calendar tomorrow afternoon and draft an email to Bob with two meeting options.
```

The PI-agent plans `calendar/search_availability` then `email/draft`, discovers
both agents through the fake Directory, calls both over A2A HTTP transport, and
returns available slots plus a draft email. `email/send` is represented as a
pending confirmation requirement and is not executed automatically.

## Milestone 3B Geo + Weather + Intelligence Demo

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_geo_weather_intelligence_demo
```

The demo prompt is:

```text
Analyze this geospatial photo and tomorrow's weather near Austin, then give me a situation summary and plan.
```

The PI-agent plans `image/geospatial_analysis`, `weather/forecast`, then
`intelligence/situation_summary`. It passes a mock image URI reference rather
than inline image bytes, carries Photo/Geospatial and Weather structured outputs
as source context, and returns the Intelligence Agent's summary, risks,
recommended plan, assumptions, and source artifact references.

## Milestone 3C Coding Demo

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_coding_demo
```

The demo sends two PI fallback messages:

```text
Review the current repo and suggest what coding task should be done next.
Fix the failing test in this repo.
```

The first message plans `code/repository_inspection`, discovers the Coding Agent
through the fake Directory, calls it over A2A HTTP transport, and returns a
deterministic repo summary plus a recommended next task. The second message
plans `code/bug_fix`, discovers the Coding Agent, and stops at policy with a
pending confirmation plus a proposed action. The mock Coding Agent is a contract
fixture only: it does not run shell commands, edit files, create commits, or
apply patches.

## Milestone 4A Directory Publication

Fake ADS is explicit. Use it only with `AGENT_NETWORK_FAKE_ADS=true` or test code
that passes `DirectoryClient(fake_ads=True)`. Real ADS mode never falls back to
fake in-memory records.

Dry-run all sample Agent Cards without publishing:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents --dry-run
```

Publish all six sample Agent Cards to fake ADS:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents
```

Publish the live Email Agent card to a real local ADS daemon through `dirctl`:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
dirctl daemon start
AGENT_NETWORK_FAKE_ADS=false \
ADS_BACKEND=cli \
ADS_SERVER_ADDR=localhost:8888 \
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json
```

Real ADS mode never falls back to fake records. `ADS_BACKEND=cli` is the default
real backend and calls `dirctl` directly, so the PI/A2A Python environment does
not need the Python `agntcy-dir` SDK to import. `ADS_BACKEND=sdk` remains
optional; in this local environment it may still fail if the installed
`agntcy-dir` generated protobuf code and `protobuf` runtime are incompatible.

The real CLI publish path is verified: after `dirctl push`, it pulls the CID,
submits `dirctl routing publish`, then polls `dirctl routing list --cid <cid>`.
The CLI reports success only after the CID appears in the routing index. If ADS
stores the record but does not route it, publishing fails loudly with the CID,
publication output, routing diagnostics, and any readable local publication
status.

## Milestone 4A Endpoint Validation

Validate the Weather mock A2A contract locally without a separately running HTTP
server:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/weather_agent.card.json \
  --skill weather/forecast \
  --prompt "What is the weather near Austin?" \
  --mock-agent weather
```

Validate a separately running external A2A HTTP endpoint:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card path/to/agent.card.json \
  --skill weather/forecast \
  --prompt "What is the weather near Austin?" \
  --endpoint http://host:port/
```

Endpoint validation does not publish to ADS unless `--publish` is supplied. In
`--mock-agent weather` mode, the card URL is treated as the declared locator and
the validator uses an ASGI-backed mock transport instead of requiring the URL to
be externally reachable.

See `v2_agent_network/docs/REAL_AGENT_INTEGRATION.md` for real-agent onboarding
requirements.

## Milestone 4A requested_skill + Seventh-Agent Acceptance

`POST /messages` accepts an optional `requested_skill` field. This field is a
capability hint, not an endpoint override. The PI-agent rejects raw endpoint
URLs in `requested_skill`; callers must publish an Agent Card/OASF record and
let PI discover the agent through the Directory.

When `requested_skill` is present, PI still follows the normal selection path:
it calls `DirectoryClient.search_agents`, selects only from discovered agents,
evaluates the same policy rules used for inferred skills, and invokes allowed
agents only through `A2AClient` and the HTTP transport. Unknown requested skills
with no discovered capable agent return a clear `no capable agent found`
response.

Confirmation-required requested skills do not auto-invoke. For example,
`email/send` and `code/bug_fix` return `confirmation_required` with a pending
confirmation instead of calling the selected agent.

The Checkpoint 3 acceptance test adds a test-only Logistics Agent with
`logistics/route_planning`, publishes its Agent Card through `DirectoryClient`,
and sends PI a request with `requested_skill="logistics/route_planning"`. This
proves a seventh agent can be onboarded dynamically without adding Logistics
endpoint logic to PI routing code or directly importing Logistics mock logic.

## Milestone 4B Real Email Agent Integration

The Gmail-backed Email Agent is an external A2A server. PI is also a server when
run with Uvicorn. `DirectoryClient` is a client used by PI and CLI tools; it is
not a server or persistent registry. Fake ADS is an in-memory per-process
backend, so `publish_agents.py` validates and publishes only for the current CLI
process. Real ADS is the shared discovery service for separately running
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

1. Direct A2A endpoint validation: use a known URL with
   `validate_agent_endpoint.py --endpoint`. This sends a safe A2A task directly
   to the endpoint and performs no discovery unless `--publish` is supplied.
2. Real ADS local discovery: run `dirctl daemon start`, publish the Email Agent
   card to ADS, search ADS by skill, and let PI discover the agent from ADS.
3. Fake ADS: test-only, in-memory, per-process fallback. It is useful for unit
   tests and demos, but it is not a shared registry and is not the final
   discovery proof.

Run PI with the live Email Agent card loaded into its own fake Directory:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true \
PI_AGENT_CARD_PATHS=v2_agent_network/configs/agent_cards/email_agent.card.json \
uvicorn v2_agent_network.services.pi_agent.main:create_app --factory --host 0.0.0.0 --port 8080
```

Or use the helper:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_pi_agent_server \
  --fake-ads \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json
```

Safely validate the live Email Agent endpoint without discovery or publishing:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json \
  --skill email/current_time \
  --prompt "What time is it for the email agent?" \
  --endpoint http://10.229.117.223:9103/
```

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json \
  --skill email/draft \
  --prompt "Draft a short test email to myself, but do not create a Gmail draft and do not send it." \
  --endpoint http://10.229.117.223:9103/
```

For `email/draft`, validation and demos set `create_gmail_draft=false`. Do not
automate `email/send`, calendar event creation, Google Doc creation/update, or
Google Sheet writes. Those skills must return `confirmation_required` unless a
manual, explicit confirmation path is added later.

Safe PI real-email demo:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_real_email_agent_demo
```

Override the endpoint if needed:

```bash
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_real_email_agent_demo \
  --endpoint http://10.229.117.223:9103/
```

The repository still includes a placeholder mock Email Agent fixture for
regression tests. The real-email demo publishes only the live Email Agent card
and asserts that PI selected the live card endpoint and provider organization.

## Milestone 4C Real ADS Email Discovery

Start a local real ADS daemon:

```bash
# install dirctl first if `command -v dirctl` is empty
dirctl daemon start
```

If this machine should accept remote publishes on the LAN, use the managed proxy
instead of an ad-hoc background command:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.manage_ads_lan start \
  --listen-host 10.229.117.37 \
  --listen-port 8888
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.manage_ads_lan status
```

Publish the live Email Agent record into ADS. This command now waits for routing
index verification before reporting success:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=false \
ADS_BACKEND=cli \
ADS_SERVER_ADDR=localhost:8888 \
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json
```

Search routed ADS entries with an official routing hint used by the Email Agent
record:

```bash
dirctl --server-addr localhost:8888 routing search \
  --skill natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search \
  --output json
```

`dirctl search` reads stored records and can return stale or orphaned CIDs.
Live discovery uses the routing index. PI searches the requested custom skill
and configured ADS routing hints through `dirctl routing search` and
`dirctl routing list`, pulls routed records, and post-filters by custom skill
tags such as `email/current_time`. The local-store fallback is disabled by
default and is only enabled when `ADS_ALLOW_LOCAL_STORE_FALLBACK=true`.

Diagnose stored-but-not-routed records:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.ads_diagnostics \
  --server-addr localhost:8888
```

The diagnostic labels CIDs as `healthy` (stored and routed), `queued` (publish
submitted but not indexed), `orphaned` (stored with no publication row), or
`stale` (stored but currently unrouted). If ADS is stuck with a queued CID, the
host operator can deliberately repair routing:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.repair_ads_routing \
  --server-addr localhost:8888 \
  --cid <cid>
```

Add `--restart-daemon-if-stuck` only when you intentionally want the repair tool
to restart the local `dirctl` daemon and re-check the routing index.

For the shared LAN ADS host, use the managed launcher with the fast-routing
daemon config. It keeps the local SQLite/OCI state but reduces the publication
and indexer intervals to 5 seconds:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
# If a daemon is already running with old defaults, do this during a quiet window:
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.manage_ads_lan \
  --listen-host 10.229.117.37 \
  stop --stop-daemon
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.manage_ads_lan \
  --listen-host 10.229.117.37 \
  start
```

For an additional no-restart guard, run the routing watcher on the ADS host. It
listens for ADS publish/unpublish events and retries missing published CIDs
without restarting the daemon unless explicitly requested:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.watch_ads_routing \
  --server-addr 10.229.117.37:8888
```

Run the safe PI -> real ADS -> live Email Agent demo:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=false \
ADS_BACKEND=cli \
ADS_SERVER_ADDR=localhost:8888 \
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_real_ads_email_discovery_demo \
  --skill email/current_time \
  --prompt "What time is it for the email agent?"
```

Run the policy-blocked send safety test:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 v2_agent_network/.venv/bin/python -m pytest \
  v2_agent_network/tests/test_milestone4c_real_ads_cli.py::test_requested_skill_email_send_requires_confirmation_before_a2a -q
```

## Milestone 5A LLM PI Tool Loop

The PI Agent can now use OpenAI as a ReAct-style tool-loop agent when
`PI_LLM_ENABLED=true` and `PI_AGENT_MODE=tool_loop`. The LLM sees protected
tool schemas, not raw endpoint authority. It can ask PI to discover agents,
inspect a discovered Agent Card, call a discovered agent skill, read context,
request confirmation, ask the user, or produce a final answer. PI still owns
DirectoryClient search, policy checks, confirmation state, and A2A transport.

`PI_AGENT_MODE=legacy` keeps the older one-shot workflow planner available for
debugging and regression tests.

Required server-side configuration:

```bash
OPENAI_API_KEY=...
PI_LLM_ENABLED=true
PI_LLM_PROVIDER=openai
PI_LLM_MODEL=gpt-5.5
PI_LLM_REASONING_EFFORT=low
PI_LLM_VERBOSITY=low
PI_LLM_MAX_OUTPUT_TOKENS=1200
PI_LLM_TIMEOUT_SECONDS=30
PI_LLM_ALLOW_DETERMINISTIC_FALLBACK=false
PI_LLM_USE_FOR_FINAL_SUMMARY=true
PI_AGENT_MODE=tool_loop
AGENT_NETWORK_FAKE_ADS=false
ADS_BACKEND=cli
ADS_SERVER_ADDR=localhost:8888
```

Keep `OPENAI_API_KEY` on the server side only. If `PI_LLM_ENABLED=true` and the
key is missing, PI fails loudly at startup. If `PI_LLM_ENABLED=false`, the
existing deterministic router remains the default.

For daily use, copy the local env template and put your key there:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
cp v2_agent_network/.env.pi.example v2_agent_network/.env.pi.local
$EDITOR v2_agent_network/.env.pi.local
```

Then start PI with one command:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/scripts/run_pi_llm_server.sh
```

Open the minimum chat UI at:

```text
http://localhost:8080/
```

For an email-send request, PI uses the tool loop to discover and inspect the
Email Agent, asks for confirmation before the side-effecting `email/send`, and
a follow-up `yes` in the same chat context resumes the stored pending tool
call instead of replanning from scratch.

`v2_agent_network/.env.pi.local` is ignored by git. You can also point the
launcher at a different env file:

```bash
PI_ENV_FILE=/path/to/pi.env v2_agent_network/scripts/run_pi_llm_server.sh
```

For debugging, set `PI_LOG_LEVEL=DEBUG` in `v2_agent_network/.env.pi.local` or
inline:

```bash
PI_LOG_LEVEL=DEBUG v2_agent_network/scripts/run_pi_llm_server.sh
```

The server logs agent mode, tool calls, discovered ADS CIDs, selected agent
endpoint, policy decision, A2A task id/state, artifact count, and errors. It
does not log `OPENAI_API_KEY`.

Run PI with the LLM planner over real ADS:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
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

Safe LLM PI demo after publishing the live Email Agent to real ADS:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
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
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_llm_pi_email_demo \
  --prompt "What time is it for the email agent?" \
  --expected-skill email/current_time
```

Blocked-send safety check:

```bash
curl -X POST http://localhost:8080/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo-user",
    "channel": "api",
    "text": "Send an email to Bob saying hello."
  }'
```

Expected result: the LLM may plan `email/send`, but PI returns
`confirmation_required=true`, does not invoke A2A, and sends no email.

For natural-language send requests, PI first asks the LLM to extract safe draft
inputs such as recipient, subject, and body, then calls `email/draft` through
A2A. `email/send` is still blocked until an explicit confirmation request.

```bash
curl -X POST http://localhost:8080/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo-user",
    "channel": "api",
    "text": "Can you send an email to wyy0115@outlook.com and ask what is for dinner?"
  }'
```

If the draft is correct and you want to actually send it, confirm the returned
`context_id`. This is the point where the live Email Agent may perform the
external Gmail side effect:

```bash
curl -X POST http://localhost:8080/confirmations \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo-user",
    "channel": "api",
    "context_id": "<context_id from /messages>",
    "skill": "email/send",
    "confirmed": true
  }'
```

Run the v2 tests with pytest plugin autoload disabled. This avoids
loading unrelated system plugins from the ROS environment.

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 v2_agent_network/.venv/bin/python -m pytest v2_agent_network/tests -q
```

Print the dependency report:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.print_versions
```

Known local versions:

- `a2a-sdk==0.3.2`
- `agntcy-app-sdk==0.4.6`
- `agntcy-dir==1.2.1`
- `protobuf==6.33.6`
- `pydantic==2.12.5`
- `fastapi==0.136.1`

Compatibility notes:

- The A2A boundary uses installed `a2a-sdk` models. A new `message/send` request
  omits `message.task_id`; in `a2a-sdk==0.3.2`, setting it means "continue an
  existing task" and the server rejects unknown task IDs.
- The installed SDK's `a2a.client.A2AClient` is deprecated, so later milestones
  should move to the SDK's `ClientFactory` JSON-RPC path if the project upgrades
  the A2A client layer.
- Real ADS uses `ADS_BACKEND=cli` by default and calls `dirctl`, which avoids
  importing the Python `agntcy-dir` SDK in the PI/A2A runtime. The optional
  `ADS_BACKEND=sdk` path can still fail loudly with `DirectoryUnavailable` if
  the installed `agntcy-dir` generated protobuf code and `protobuf` runtime are
  incompatible.

Later milestones will add Telegram and WhatsApp adapters, observability, Docker
Compose, and SLIM/NATS transports.
