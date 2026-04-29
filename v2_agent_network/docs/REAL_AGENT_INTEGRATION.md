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
process with `PI_AGENT_CARD_PATHS` or `run_pi_agent_server.py --card`, or use
real ADS once the dependency issue below is fixed.

```text
User/API
  -> PI Agent server
  -> DirectoryClient
  -> ADS or fake ADS
  -> A2AClient
  -> external Email Agent A2A server
```

Dry-run local validation:

```bash
AGENT_NETWORK_FAKE_ADS=false v1_classification/.venv/bin/python -m v2_agent_network.scripts.publish_agents --dry-run
```

Publish sample records to fake ADS:

```bash
AGENT_NETWORK_FAKE_ADS=true v1_classification/.venv/bin/python -m v2_agent_network.scripts.publish_agents
```

Publish to real ADS only after dependency compatibility is fixed:

```bash
AGENT_NETWORK_FAKE_ADS=false ADS_URL=localhost:8888 v1_classification/.venv/bin/python -m v2_agent_network.scripts.publish_agents
```

In this environment, `agntcy-dir==1.2.1` currently fails to import because its
generated protobuf code expects a newer protobuf runtime than
`protobuf==6.33.6`. Milestone 4A intentionally does not change dependencies.

## Endpoint Validation

Local mock validation without a running HTTP server:

```bash
v1_classification/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/weather_agent.card.json \
  --skill weather/forecast \
  --prompt "What is the weather near Austin?" \
  --mock-agent weather
```

External endpoint validation:

```bash
v1_classification/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
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
v1_classification/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json \
  --skill email/current_time \
  --prompt "What time is it for the email agent?" \
  --endpoint http://10.229.117.223:9103/
```

```bash
v1_classification/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
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
v1_classification/.venv/bin/python -m v2_agent_network.scripts.run_pi_agent_server \
  --fake-ads \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json
```

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
- If real ADS fails with `DirectoryUnavailable`, inspect the included
  `protobuf` and `agntcy-dir` versions and resolve dependency compatibility in a
  separate dependency milestone.
- If the PI cannot discover an agent, publish the card/record first and confirm
  the requested skill is present as an official OASF skill or custom skill tag.
