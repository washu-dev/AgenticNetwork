# V2 Agent Network Migration Specification

## Goal

Build a new AGNTCY-based agent network where a PI agent receives tasks from Telegram, WhatsApp, or HTTP API, discovers specialized agents through AGNTCY Directory / ADS, and delegates work through A2A-compatible task communication.

This is a major shift from the existing v1 image-classification demo. The v2 system is **not** an image classifier.

## Target architecture

```text
PI user on Telegram / WhatsApp / HTTP API
  -> Messaging ingress webhook
  -> PI Agent / Coordinator
  -> AGNTCY Directory / ADS discovery
  -> Agent selection / routing policy
  -> A2A task call to specialized agent
  -> A2A task status / artifacts
  -> PI Agent response synthesis
  -> Telegram / WhatsApp / HTTP API reply
```

## Specialized agents in the network

The specialized agents are being implemented by other people. Do not implement their internal intelligence.

The infrastructure should support these agent types:

- Email agent
- Calendar agent
- Situational awareness / battle-plan / intelligence agent
- Photo agent, possibly geospatial / VisNet-style
- Weather agent
- Coding agent

Treat these as independently owned remote services. Implement only:

- interface contracts
- sample Agent Cards
- OASF-compatible record conversion
- discovery wrappers
- mock A2A servers for local demo
- A2A client/server abstractions
- PI-agent routing, policy, state, logging, and demo scaffolding

## Core implementation principle

The deliverable is the infrastructure that lets independently written agents join the network:

```text
publish -> discover -> route -> A2A task -> status/artifacts -> policy -> response
```

A new real agent should be able to join by:

1. creating an Agent Card and/or OASF-compatible record,
2. publishing it to AGNTCY Directory / ADS,
3. exposing an A2A endpoint, and
4. being discovered by the PI agent without editing PI-agent routing code.

## Recommended repo structure

Create a new implementation area rather than mutating the old image-classification app beyond recognition.

```text
v2_agent_network/
  services/
    common/
    pi_agent/
    gateway/
    directory/
    a2a/
    messaging/
    state/
    observability/
    mock_agents/
  configs/
    agent_cards/
    oasf_records/
  scripts/
  tests/
  docs/
  docker-compose.yml
  README.md
```

Keep the old image-classification implementation available as `v1_classification/` or legacy code. Do not delete it unless explicitly requested.

The new `v2_agent_network/` system should be the default documented demo.

## Implementation goals

Implement the infrastructure for an AGNTCY agent network with these goals:

1. Agents can publish metadata to AGNTCY Directory / ADS.
2. The PI agent can discover agents dynamically through ADS.
3. The PI agent does not hardcode agent endpoints.
4. The PI agent can route user tasks to specialized agents based on declared capabilities.
5. The PI agent communicates with agents through an A2A-compatible task/message/artifact abstraction.
6. Telegram and WhatsApp ingress are supported through webhook adapters.
7. The system supports task state, context IDs, message IDs, task IDs, artifacts, logs, and traces.
8. The demo can run even if the real specialized agents are not ready, using mock A2A agents.
9. Adding a new agent should require publishing a card/record and starting its endpoint, not editing PI-agent routing code.

## Protocol boundaries

Respect these protocol boundaries.

### AGNTCY Directory / ADS

Use for:

- publishing agent metadata
- storing agent metadata
- resolving agent records
- discovering agents by skill, domain, modality, and constraints
- representing agents through OASF-compatible records and/or A2A Agent Cards

Do not use ADS as the runtime task-message bus.

### A2A

Use for agent-to-agent task communication.

The PI agent is an A2A client when it delegates to Email, Calendar, Weather, Photo/Geospatial, Intelligence, or Coding agents.

Each specialized agent should be represented as an A2A server.

A2A payloads should preserve these fields:

- `context_id`
- `task_id`
- `message_id`
- `parts`
- `artifacts`
- `status`

Do not call custom JSON messages “A2A” unless they preserve A2A task/message/artifact semantics.

### Transport

Transport is replaceable.

Acceptable first version:

- HTTP A2A transport, if simplest and spec-aligned.

Optional later versions:

- SLIM / SlimRPC through AGNTCY App SDK.
- NATS transport, isolated behind the A2A abstraction.

Rules:

- Prefer AGNTCY App SDK transport abstraction if practical.
- Prefer SLIM/SlimRPC for native AGNTCY-style A2A if available in the repo/runtime.
- NATS may be used as a transport, but isolate it behind an A2A client/server abstraction.
- The PI agent should not know whether a remote agent uses HTTP, SLIM, or NATS internally. It should use a normalized A2A client interface.

### Messaging channels

Telegram and WhatsApp are ingress/egress channels only.

- Telegram/WhatsApp messages become PI-agent tasks.
- Do not mix Telegram/WhatsApp message schemas with A2A schemas.
- Keep channel adapters isolated.

## Core models

Create shared Pydantic models in:

```text
v2_agent_network/services/common/models.py
```

Required models:

### UserMessage

Fields:

- `id`
- `channel`: `telegram | whatsapp | api`
- `sender_id`
- `sender_display_name` optional
- `text`
- `attachments`
- `received_at`

### PIContext

Fields:

- `context_id`
- `user_id`
- `channel`
- `original_message_id`
- `current_goal`
- `created_at`
- `updated_at`
- `conversation_history`
- `active_tasks`

### AgentCapability

Fields:

- `name`
- `description`
- `skill_tags`
- `domain_tags`
- `input_modes`
- `output_modes`
- `required_auth`
- `risk_level`
- `examples`

### DiscoveredAgent

Fields:

- `agent_id`
- `name`
- `organization`
- `description`
- `skills`
- `domains`
- `input_modes`
- `output_modes`
- `endpoint`
- `transport`
- `auth`
- `trust_level`
- `source_record_id`
- `source_card_url` optional
- `raw_record` optional

### A2APart

Fields:

- `kind`: `text | data | file | image | audio | video`
- `content_type`
- `text` optional
- `data` optional
- `uri` optional
- `metadata`

### A2AMessage

Fields:

- `message_id`
- `role`: `user | agent`
- `parts`
- `created_at`

### A2AArtifact

Fields:

- `artifact_id`
- `name`
- `content_type`
- `parts`
- `uri` optional
- `metadata`

### A2ATaskRequest

Fields:

- `task_id` optional
- `context_id`
- `target_agent_id`
- `skill`
- `user_goal`
- `messages`
- `metadata`

### A2ATaskStatus

Fields:

- `task_id`
- `context_id`
- `state`: `submitted | working | input_required | completed | failed | canceled`
- `message` optional
- `updated_at`

### A2ATaskResult

Fields:

- `task_id`
- `context_id`
- `state`
- `messages`
- `artifacts`
- `structured_output` optional
- `error` optional

### RoutingDecision

Fields:

- `context_id`
- `user_goal`
- `selected_agents`
- `rejected_agents`
- `reason`
- `confidence`
- `requires_human_confirmation`

### AgentInvocationRecord

Fields:

- `context_id`
- `task_id`
- `agent_id`
- `skill`
- `request_summary`
- `status`
- `artifacts`
- `started_at`
- `completed_at`
- `error` optional

## Agent Cards and OASF records

Create:

```text
v2_agent_network/configs/agent_cards/
```

Add sample Agent Card JSON files for:

1. `email_agent.card.json`
2. `calendar_agent.card.json`
3. `intelligence_agent.card.json`
4. `photo_geospatial_agent.card.json`
5. `weather_agent.card.json`
6. `coding_agent.card.json`

Each card must include:

- agent id
- name
- organization
- description
- version
- skills
- domains
- input modes
- output modes
- endpoint locator
- transport
- auth requirements
- risk level
- examples

Use capability names that are specific enough for routing.

### Suggested skills

Email agent:

- `email/search`
- `email/read`
- `email/draft`
- `email/send`
- `email/summarize_thread`

Calendar agent:

- `calendar/search_availability`
- `calendar/create_event`
- `calendar/update_event`
- `calendar/respond_invitation`
- `calendar/summarize_schedule`

Weather agent:

- `weather/current_conditions`
- `weather/forecast`
- `weather/alerts`
- `weather/route_weather_assessment`

Photo / geospatial agent:

- `image/geospatial_analysis`
- `image/object_detection`
- `image/location_inference`
- `image/satellite_interpretation`
- `image/photo_forensics_basic`

Situational awareness / battle-plan / intelligence agent:

- `intelligence/situation_summary`
- `intelligence/risk_assessment`
- `intelligence/course_of_action`
- `intelligence/entity_tracking`
- `intelligence/report_generation`

Coding agent:

- `code/repository_inspection`
- `code/bug_fix`
- `code/pr_review`
- `code/test_generation`
- `code/documentation`

Create:

```text
v2_agent_network/configs/oasf_records/
```

Generate OASF-compatible records from the Agent Cards where possible.

Create:

```text
v2_agent_network/services/directory/card_oasf_converter.py
```

Functions:

- `load_agent_card(path) -> AgentCard`
- `agent_card_to_oasf_record(card) -> dict`
- `oasf_record_to_agent_card(record) -> AgentCard`
- `validate_agent_card(card) -> None`
- `validate_oasf_record(record) -> None`

Do not overfit to the six sample agents. This must work for future agents.

## Directory / ADS wrapper

Create:

```text
v2_agent_network/services/directory/client.py
```

Implement a `DirectoryClient` abstraction.

Required methods:

```python
publish_agent_card(card: AgentCard) -> PublishedRecord
publish_oasf_record(record: dict) -> PublishedRecord
search_agents(
    skill: str | None,
    domain: str | None,
    input_mode: str | None,
    output_mode: str | None,
    constraints: dict | None
) -> list[DiscoveredAgent]
resolve_agent(agent_id_or_record_id: str) -> DiscoveredAgent
healthcheck() -> bool
```

The implementation should support:

1. Real AGNTCY Directory / ADS backend.
2. Fake in-memory backend for tests only.

Important:

- The fake backend must only be enabled with `AGENT_NETWORK_FAKE_ADS=true`.
- If fake ADS is disabled and ADS is unavailable, fail loudly.
- Do not silently fall back to fake discovery.

Configuration:

- `ADS_URL`
- `ADS_NAMESPACE`
- `ADS_ORG_ID`
- `AGENT_NETWORK_FAKE_ADS`
- `AGENT_CARD_DIR`
- `OASF_RECORD_DIR`

Create:

```text
v2_agent_network/scripts/publish_agents.py
```

This script should:

1. Load all cards from `configs/agent_cards`.
2. Validate them.
3. Convert to OASF-compatible records if needed.
4. Publish to ADS.
5. Print the published record IDs.
6. Fail clearly if ADS is unavailable and fake mode is not enabled.

## A2A client and server abstractions

Create:

```text
v2_agent_network/services/a2a/client.py
v2_agent_network/services/a2a/server.py
v2_agent_network/services/a2a/transport.py
```

The PI agent should use only `A2AClient`.

### A2AClient methods

```python
send_task(agent: DiscoveredAgent, request: A2ATaskRequest) -> A2ATaskResult
get_task_status(agent: DiscoveredAgent, task_id: str) -> A2ATaskStatus
cancel_task(agent: DiscoveredAgent, task_id: str) -> A2ATaskStatus
stream_task_events(agent: DiscoveredAgent, task_id: str) -> AsyncIterator
```

### A2AServer

`A2AServer` should be usable by mock agents and future real agents.

Handlers or endpoints:

- submit task
- get task status
- get task result/artifacts
- cancel task
- healthcheck
- agent card endpoint

### Transport

- Implement HTTP transport first unless the repo already has working AGNTCY App SDK transport.
- Keep transport replaceable.
- Add TODO or optional implementation for SLIM/NATS using AGNTCY App SDK.
- Do not leak transport-specific code into the PI agent.

### A2A task semantics

- Generate `task_id` if missing.
- Preserve `context_id` across all calls.
- Store messages and artifacts.
- Support `completed`, `failed`, `input_required`, and `working` states.
- Return `structured_output` plus artifacts.

## PI agent

Create:

```text
v2_agent_network/services/pi_agent/main.py
v2_agent_network/services/pi_agent/planner.py
v2_agent_network/services/pi_agent/router.py
v2_agent_network/services/pi_agent/policy.py
v2_agent_network/services/pi_agent/state.py
```

The PI agent is the central coordinator.

Responsibilities:

1. Receive normalized `UserMessage` objects from messaging ingress.
2. Maintain conversation context.
3. Interpret the user goal.
4. Decide whether the task needs one agent or multiple agents.
5. Search AGNTCY Directory for candidate agents.
6. Rank candidates.
7. Ask for human confirmation when the action is risky.
8. Create A2A task requests.
9. Send tasks to selected agents.
10. Track task status and artifacts.
11. Compose the final reply back to the user.

Do not implement a complex LLM planner initially. Implement a deterministic router first, with optional LLM routing behind a feature flag.

### Router requirements

- Map user intent to one or more skills.
- Search ADS by skill/domain/input/output mode.
- Rank by:
  - exact skill match
  - domain match
  - input/output compatibility
  - trust_level
  - availability
  - policy constraints
- Return a `RoutingDecision`.

### Example routing

- `"Email John and ask if he is free tomorrow"` -> Email agent + Calendar agent
- `"Schedule a meeting with Alice next week"` -> Calendar agent, optionally Email agent
- `"What is the weather near this location tomorrow?"` -> Weather agent
- `"Analyze this satellite photo"` -> Photo/geospatial agent
- `"Summarize the current situation and propose a plan"` -> Intelligence agent
- `"Fix the failing test in this repo"` -> Coding agent
- `"Check my calendar and draft an email with a plan"` -> Calendar agent + Email agent
- `"Given this photo and forecast, make a battle plan"` -> Photo/geospatial agent + Weather agent + Intelligence agent

### Policy requirements

Read-only actions can execute directly.

Write actions require confirmation by default:

- sending email
- creating/updating/deleting calendar events
- running code that modifies repo state
- posting external messages

The policy module should return:

- `allowed`
- `requires_confirmation`
- `denied`
- `reason`

### Multi-step example

```text
User: "Can we meet tomorrow afternoon? Email Bob options."

1. PI agent calls Calendar agent for availability.
2. PI agent calls Email agent to draft message.
3. PI agent asks user for approval before sending.
```

## Messaging ingress

Create:

```text
v2_agent_network/services/messaging/main.py
v2_agent_network/services/messaging/telegram.py
v2_agent_network/services/messaging/whatsapp.py
v2_agent_network/services/messaging/api.py
```

Implement messaging ingress as adapters.

Each adapter should:

1. Receive raw webhook payload.
2. Validate signature/token if configured.
3. Normalize into `UserMessage`.
4. Forward `UserMessage` to PI agent.
5. Send PI-agent response back to the channel.

### Telegram

- Implement webhook endpoint.
- Support text messages.
- Support attachments as file references if practical.
- Config:
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_WEBHOOK_SECRET`

### WhatsApp

- Implement webhook verification endpoint.
- Implement inbound message endpoint.
- Support text messages.
- Support media references if practical.
- Config:
  - `WHATSAPP_VERIFY_TOKEN`
  - `WHATSAPP_ACCESS_TOKEN`
  - `WHATSAPP_PHONE_NUMBER_ID`

### API fallback

Add a simple HTTP endpoint:

```http
POST /messages
```

Body:

```json
{
  "sender_id": "demo-user",
  "text": "Check my calendar and draft an email with a plan.",
  "attachments": []
}
```

This API fallback is important for local testing without Telegram/WhatsApp credentials.

## Mock agents

Create mock A2A agents for local demo only:

```text
v2_agent_network/services/mock_agents/email_agent.py
v2_agent_network/services/mock_agents/calendar_agent.py
v2_agent_network/services/mock_agents/weather_agent.py
v2_agent_network/services/mock_agents/photo_geospatial_agent.py
v2_agent_network/services/mock_agents/intelligence_agent.py
v2_agent_network/services/mock_agents/coding_agent.py
```

Each mock agent should:

- expose its Agent Card
- implement the A2A server interface
- return deterministic structured outputs
- include enough behavior to prove routing and multi-agent composition
- not pretend to be production-ready

### Mock outputs

Email agent:

- search/read returns fake thread summaries
- draft returns a draft email artifact
- send returns `requires_confirmation` unless explicitly confirmed

Calendar agent:

- availability returns fake time slots
- create_event returns event artifact but requires confirmation

Weather agent:

- forecast returns fake or optionally real weather if configured
- include location, time range, conditions, risk

Photo/geospatial agent:

- return fake geospatial/photo assessment
- include detected terrain/features if a photo attachment is provided

Intelligence agent:

- combine inputs from weather/photo/calendar/email/coding outputs
- return situation summary, risks, and recommended plan

Coding agent:

- return repo inspection summary or mock PR/review artifact
- no actual code modification unless explicitly enabled

Important:

- Mock agents are for infrastructure demo only.
- Keep real-agent integration points clear.

## State and artifacts

Create:

```text
v2_agent_network/services/state/store.py
v2_agent_network/services/state/models.py
```

Implement a simple persistent state store.

Acceptable initial choices:

- SQLite for task/conversation state
- local filesystem or MinIO for artifacts
- environment flag to choose

State to persist:

- user messages
- PI contexts
- routing decisions
- discovered agents used
- A2A task requests
- A2A task results
- artifacts
- confirmation decisions
- errors

Artifact handling:

- Do not pass large images/files inline through every message.
- Store files in object storage or local artifact store.
- Pass file references/URIs in A2A parts.
- Validate file references before use.

## Observability

Create:

```text
v2_agent_network/services/observability/tracing.py
v2_agent_network/services/observability/logging.py
```

Every user request should produce a trace with:

- `context_id`
- inbound channel
- original user message
- discovered agents
- selected agents
- routing decision
- policy decision
- A2A task IDs
- task states
- artifacts
- final response
- errors

Use OpenTelemetry if already present in the repo. Otherwise implement structured JSON logs first.

Do not block implementation on Grafana/ClickHouse. Make advanced observability optional.

## Docker Compose

Create or update:

```text
v2_agent_network/docker-compose.yml
```

Services:

- `pi-agent`
- `messaging-gateway`
- `ads` / directory service if available locally
- optional NATS or SLIM if transport uses it
- state database, if not SQLite
- object/artifact store, if using MinIO
- `mock-email-agent`
- `mock-calendar-agent`
- `mock-weather-agent`
- `mock-photo-geospatial-agent`
- `mock-intelligence-agent`
- `mock-coding-agent`

Environment:

- `AGENT_NETWORK_FAKE_ADS=false` by default
- `A2A_TRANSPORT=http` by default

Optional:

- `A2A_TRANSPORT=slim`
- `A2A_TRANSPORT=nats`
- `TELEGRAM_*`
- `WHATSAPP_*`

Local dev mode:

- Should run without Telegram/WhatsApp using the API fallback.
- Should run with fake ADS only when explicitly enabled.

## Demo script

Create:

```text
v2_agent_network/scripts/run_demo.py
```

The demo should:

1. Start or check required services.
2. Publish all sample Agent Cards / OASF records.
3. Send test messages through the API fallback.
4. Show which agents were discovered.
5. Show routing decisions.
6. Show A2A task IDs.
7. Show returned artifacts.
8. Show final PI-agent response.

### Demo messages

A. `"What is the weather tomorrow near Austin and should I plan outdoor field work?"`

Expected:

- Weather agent
- possibly Intelligence agent

B. `"Look at this geospatial photo and summarize what it means for tomorrow's plan."`

Expected:

- Photo/geospatial agent
- Weather agent
- Intelligence agent

C. `"Check my calendar for tomorrow afternoon and draft an email to Bob with two meeting options."`

Expected:

- Calendar agent
- Email agent
- confirmation required before sending

D. `"Given the current situation, make me a battle plan."`

Expected:

- Intelligence agent
- possibly Weather/Photo depending on context

E. `"Review the current repo and suggest what coding task should be done next."`

Expected:

- Coding agent

F. `"Schedule a meeting, email the plan, and include weather constraints."`

Expected:

- Calendar agent
- Weather agent
- Email agent
- policy confirmation gate

The script should print a concise trace for each message.

## Tests

Add tests under:

```text
v2_agent_network/tests/
```

Required tests:

1. Agent Card validation
2. OASF conversion
3. Directory fake backend publish/search/resolve
4. Directory real backend healthcheck, skipped if ADS unavailable
5. Router maps user text to expected skills
6. Router uses discovery results instead of hardcoded endpoints
7. Policy marks read-only tasks as allowed
8. Policy marks write actions as confirmation-required
9. A2A client can call mock agent
10. A2A task preserves context_id and task_id
11. Multi-agent workflow:
    - calendar availability
    - email draft
    - confirmation required
12. Messaging API fallback normalizes messages
13. Telegram adapter parses text payload, with mocked auth
14. WhatsApp adapter parses text payload, with mocked auth
15. State store persists context, task, and artifact records
16. Demo script smoke test with mock agents

Do not require real Telegram/WhatsApp credentials for tests.

Do not require real specialized agents for tests.

## README

Write a new README for `v2_agent_network`.

README thesis:

> This repository implements an AGNTCY-based agent network where a PI agent receives user tasks from Telegram, WhatsApp, or HTTP API, discovers specialized agents through AGNTCY Directory / ADS, and delegates work through A2A-compatible task communication. The specialized agents can be owned by different organizations and implemented with different frameworks. The infrastructure demonstrates publication, discovery, routing, task delegation, confirmation gates, artifact handling, and observability.

README sections:

1. What this is
2. What this is not
   - Not an image classifier
   - Not a monolithic assistant
   - Not hardcoded tool calling
3. Architecture diagram
4. Components
   - PI agent
   - Messaging ingress
   - Directory / ADS
   - A2A client/server
   - Specialized agents
   - State store
   - Observability
5. Protocol boundaries
   - ADS = publish/discovery
   - A2A = task/message/artifact communication
   - SLIM/NATS/HTTP = transport
   - Telegram/WhatsApp = user ingress
6. How to run locally
7. How to publish an agent
8. How to add a new agent
9. How routing works
10. How confirmation gates work
11. How to run demo messages
12. How to replace mock agents with real agents
13. Current limitations
14. Future work

Include a clear example:

```text
To add a new Logistics Agent:
1. Create logistics_agent.card.json.
2. Publish it with scripts/publish_agents.py.
3. Start the Logistics Agent A2A server.
4. Send a task that requires logistics capability.
5. PI agent discovers it without code changes.
```

## Acceptance criteria

Implementation is acceptable only if all of the following are true:

1. `v2_agent_network/` exists and runs independently from the old image-classification demo.
2. PI agent can receive a test message through HTTP API fallback.
3. At least six sample agents have Agent Cards:
   - Email
   - Calendar
   - Weather
   - Photo/geospatial
   - Intelligence/battle-plan
   - Coding
4. Agent Cards can be published to Directory / ADS, or to explicit fake ADS in test mode.
5. PI agent discovers agents through `DirectoryClient`.
6. PI agent does not hardcode service URLs for routing.
7. PI agent delegates to mock agents through `A2AClient`.
8. A2A task calls preserve `context_id`, `task_id`, message parts, status, and artifacts.
9. Multi-agent workflows work for at least:
   - calendar + email
   - weather + intelligence
   - photo/geospatial + weather + intelligence
10. Write actions require confirmation.
11. `run_demo.py` prints discovered agents, selected agents, A2A task IDs, artifacts, and final response.
12. Tests pass for models, routing, policy, fake discovery, A2A mock calls, and message normalization.
13. README clearly explains the AGNTCY Directory + A2A architecture.

## First step before coding

Before editing files, inspect the repository and produce a short migration plan.

The migration plan must answer:

1. What parts of the old image-classification repo can be reused?
2. What parts should be deprecated or moved under legacy/v1_classification?
3. Is there already an AGNTCY Directory client?
4. Is there already an A2A client/server abstraction?
5. Is NATS currently used as arbitrary JSON messaging or true A2A-compatible transport?
6. What is the smallest set of files needed for `v2_agent_network/`?
7. What risks or missing dependencies could block implementation?

After the migration plan, proceed with implementation.

## Codex implementation note

Do not implement the specialized agents' intelligence.

The deliverable is the infrastructure that lets independently written agents join the network:

```text
publish -> discover -> route -> A2A task -> status/artifacts -> policy -> response
```

Use mock agents only to prove the infrastructure.

## External references

These links are for implementation context, not as a substitute for inspecting the current repository.

- OpenAI Codex AGENTS.md guide: https://developers.openai.com/codex/guides/agents-md
- AGNTCY Directory overview: https://docs.agntcy.org/dir/overview/
- AGNTCY ADS storage specification: https://spec.dir.agntcy.org/feat/storage-specs/draft-mp-agntcy-ads.html
- AGNTCY OASF Agent Record Guide: https://docs.agntcy.org/oasf/agent-record-guide/
- A2A Protocol specification: https://a2a-protocol.org/latest/specification/
- A2A and MCP relationship: https://a2a-protocol.org/latest/topics/a2a-and-mcp/
