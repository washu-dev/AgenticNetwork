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

This is not the v1 image-classification demo.

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

## Milestone 2 Demo

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v1_classification/.venv/bin/python -m v2_agent_network.scripts.run_weather_demo
```

## Milestone 3A Calendar + Email Demo

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v1_classification/.venv/bin/python -m v2_agent_network.scripts.run_calendar_email_demo
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
AGENT_NETWORK_FAKE_ADS=true v1_classification/.venv/bin/python -m v2_agent_network.scripts.run_geo_weather_intelligence_demo
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
AGENT_NETWORK_FAKE_ADS=true v1_classification/.venv/bin/python -m v2_agent_network.scripts.run_coding_demo
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
AGENT_NETWORK_FAKE_ADS=true v1_classification/.venv/bin/python -m v2_agent_network.scripts.publish_agents --dry-run
```

Publish all six sample Agent Cards to fake ADS:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v1_classification/.venv/bin/python -m v2_agent_network.scripts.publish_agents
```

Real ADS scaffold command:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=false ADS_URL=localhost:8888 v1_classification/.venv/bin/python -m v2_agent_network.scripts.publish_agents
```

Real ADS currently fails loudly in this environment because `agntcy-dir==1.2.1`
imports generated protobuf code that requires a newer protobuf runtime than the
installed `protobuf==6.33.6`. Milestone 4A intentionally does not change package
versions; fix AGNTCY/protobuf compatibility in a separate dependency milestone.

## Milestone 4A Endpoint Validation

Validate the Weather mock A2A contract locally without a separately running HTTP
server:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v1_classification/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/weather_agent.card.json \
  --skill weather/forecast \
  --prompt "What is the weather near Austin?" \
  --mock-agent weather
```

Validate a separately running external A2A HTTP endpoint:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v1_classification/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
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

Run the v2 tests with pytest plugin autoload disabled. This avoids
loading unrelated system plugins from the ROS environment.

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 v1_classification/.venv/bin/python -m pytest v2_agent_network/tests -q
```

Print the dependency report:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v1_classification/.venv/bin/python -m v2_agent_network.scripts.print_versions
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
- Real ADS is scaffolded in Milestone 4A, but this environment has an
  `agntcy-dir` / protobuf compatibility mismatch. Real ADS operations fail
  loudly with `DirectoryUnavailable` until dependency compatibility is fixed in
  a separate milestone.

Later milestones will add seventh-agent acceptance, Telegram and WhatsApp
adapters, observability, Docker Compose, and SLIM/NATS transports.
