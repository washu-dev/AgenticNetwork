# Milestone 4A: Real ADS + Real-Agent Onboarding

## Purpose

Milestone 4A prepares `v2_agent_network/` to onboard real external agents while keeping the existing mock agents as contract fixtures and regression tests.

Milestones 1-3C have already proven the local fake-ADS + A2A HTTP path with six mock agent types:

- Weather
- Calendar
- Email
- Photo/Geospatial
- Intelligence
- Coding

Milestone 4A should **not** implement Telegram, WhatsApp, Docker Compose, observability, persistent database state, or real SLIM/NATS transport. Those remain later milestones.

The focus is:

```text
publish -> discover -> validate endpoint -> invoke via A2A -> replace mock with real agent
```

The target integration path is:

```text
PI Agent
  -> DirectoryClient
  -> AGNTCY Directory / ADS, or explicit fake ADS in test mode
  -> discovered Agent Card / OASF record
  -> A2AClient
  -> HTTP transport
  -> mock or real external A2A server
  -> task result / artifacts
  -> PI response
```

## Current Project Context

Current accepted milestones:

- Milestone 1-2: Weather path.
- Milestone 3A: Calendar + Email workflow with confirmation requirement for `email/send`.
- Milestone 3B: Photo/Geospatial + Weather + Intelligence artifact-composition workflow.
- Milestone 3C: Coding Agent mock with read-only repository inspection and confirmation-gated repo-modifying actions.

Current known local versions:

- `a2a-sdk==0.3.2`
- `agntcy-app-sdk==0.4.6`
- `agntcy-dir==1.2.1`
- `protobuf==6.33.6`
- `pydantic==2.12.5`
- `fastapi==0.136.1`

Known constraints:

- The old image-classification prototype is not part of the ADS runtime.
- Mock agents are contract fixtures, not real intelligence.
- Fake ADS is allowed only when `AGENT_NETWORK_FAKE_ADS=true` or an explicit test constructor requests fake mode.
- Real ADS failures must be explicit. Do not silently fall back to fake ADS.
- A2A boundary must continue to use installed SDK types or explicit adapter functions, not arbitrary custom JSON.
- Large files/images should be passed by URI/reference, not inline payloads.

## Implementation Mode

The real external agents are **not required** for this milestone.

Implement 4A in **no-real-agent mode first**:

- Use existing six mock A2A agents as contract fixtures.
- Add tools that can validate mock endpoints now and real endpoints later.
- Add real ADS support or a real ADS scaffold that fails loudly when not configured.
- Add integration documentation so other teams can plug in their real agents tomorrow.
- Add a seventh-agent acceptance test using a test-only Logistics Agent mock.

Do not block the milestone on real external agents being ready.

---

# Scope

## In Scope

1. Real/fake `DirectoryClient` separation.
2. Real ADS backend support or scaffold using installed `agntcy-dir` / existing v1 ADS patterns.
3. `publish_agents.py` script.
4. `validate_agent_endpoint.py` script.
5. `docs/REAL_AGENT_INTEGRATION.md`.
6. Seventh-agent acceptance test using a test-only Logistics Agent.
7. External HTTP endpoint validation path for A2A servers.
8. README updates for fake ADS, real ADS, endpoint validation, and agent replacement.
9. Tests for all of the above.

## Out of Scope

Do not implement in this milestone:

- Telegram adapter.
- WhatsApp adapter.
- Docker Compose.
- Observability stack.
- Persistent database state.
- Real SLIM/NATS transport.
- Real agent intelligence.
- Real code-modifying Coding Agent behavior.
- Reintroducing dependencies on the old image-classification prototype.

---

# Section 1: DirectoryClient Real ADS Backend

## Goal

Complete or scaffold real AGNTCY Directory / ADS support while preserving explicit fake ADS behavior.

## Files

Update:

```text
v2_agent_network/services/directory/client.py
```

Add tests under:

```text
v2_agent_network/tests/
```

## Requirements

`DirectoryClient` must support two modes:

1. **Fake ADS mode** for tests and local demos.
2. **Real ADS mode** for publishing/discovering records through AGNTCY Directory / ADS.

Fake mode is allowed only when:

```text
AGENT_NETWORK_FAKE_ADS=true
```

or when tests explicitly instantiate fake mode.

If fake mode is false and ADS is unavailable or unconfigured, `DirectoryClient` must fail loudly with a clear exception such as `DirectoryUnavailable`.

No silent fallback.

## Required Methods

The following methods must work consistently across fake and real modes, or fail loudly in real mode if ADS is unavailable:

```python
publish_agent_card(card)
publish_oasf_record(record)
search_agents(
    skill: str | None = None,
    domain: str | None = None,
    input_mode: str | None = None,
    output_mode: str | None = None,
    constraints: dict | None = None,
)
resolve_agent(agent_id_or_record_id: str)
healthcheck()
```

## Configuration

Support these environment variables where applicable:

```text
AGENT_NETWORK_FAKE_ADS
ADS_URL
ADS_NAMESPACE
ADS_ORG_ID
ADS_API_KEY
AGENT_CARD_DIR
OASF_RECORD_DIR
```

If installed AGNTCY SDKs require different environment variables, document the actual names in the README and in `REAL_AGENT_INTEGRATION.md`.

## Implementation Notes

Before implementation, inspect:

- Existing v1 ADS utilities and publish/discovery patterns.
- Installed `agntcy-dir` APIs.
- Installed `agntcy-app-sdk` APIs.
- Current v2 `DirectoryClient` fake backend.

Recommended implementation pattern:

```text
DirectoryClient
  -> FakeDirectoryBackend
  -> RealADSDirectoryBackend
```

or equivalent internal separation.

Do not let fake records leak into real mode.

## Tests

Add/update tests for:

1. Fake backend publish/search/resolve still passes.
2. Real backend `healthcheck()` test is skipped when ADS is not configured.
3. Real backend fails loudly when `AGENT_NETWORK_FAKE_ADS=false` and ADS config is missing/unavailable.
4. No silent fallback to fake records in real mode.
5. Existing Weather, Calendar+Email, Geo+Weather+Intelligence, and Coding tests still pass.

---

# Section 2: Publish Script

## Goal

Add a script that publishes Agent Cards / OASF records to fake ADS or real ADS.

## File

Create:

```text
v2_agent_network/scripts/publish_agents.py
```

## Behavior

The script should:

1. Load Agent Cards from `v2_agent_network/configs/agent_cards` by default.
2. Accept `--card-dir` to load a different directory.
3. Accept `--card` to publish a single card.
4. Validate each Agent Card.
5. Convert each Agent Card to an OASF/ADS record.
6. Publish each record through `DirectoryClient`.
7. Print a readable table of published records.
8. Support `--dry-run` to validate and print without publishing.
9. Exit nonzero if real ADS is requested but unavailable.

## Output Table Fields

Print at least:

```text
agent_id
name
organization
skills
endpoint
transport
record_id / cid
backend: fake | real
```

## Commands

Dry run with fake ADS:

```bash
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents --dry-run
```

Publish to fake ADS:

```bash
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents
```

Real ADS example:

```bash
AGENT_NETWORK_FAKE_ADS=false ADS_URL=... v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents
```

If real ADS requires additional configuration, document it.

## Tests

Add tests for:

1. `publish_agents.py --dry-run` validates all six cards.
2. `publish_agents.py` publishes all six cards to fake ADS.
3. Real ADS publish path is skipped or fails loudly when ADS is not configured.
4. Output includes agent id, skills, endpoint, and backend.

---

# Section 3: Real-Agent Endpoint Validation Tool

## Goal

Add a script that validates whether a real external agent can join the network.

This tool should work against mock endpoints now and real agent endpoints later.

## File

Create:

```text
v2_agent_network/scripts/validate_agent_endpoint.py
```

## Purpose

Given an Agent Card and a test prompt, verify:

```text
card -> OASF conversion -> endpoint reachability -> A2A task call -> task/result/artifacts
```

This is an interface conformance check, not an intelligence evaluation.

## CLI Inputs

Support:

```text
--card path/to/agent.card.json
--skill skill/name
--prompt "test prompt"
--fake-ads
--publish
--timeout seconds
```

Optional:

```text
--endpoint override_url
--context-id context_id
--sender-id test_user
```

## Checks

The script should verify:

1. Agent Card loads and validates.
2. OASF conversion succeeds.
3. Endpoint locator exists.
4. Agent Card endpoint is reachable if declared separately.
5. A2A endpoint is reachable.
6. `A2AClient` can send a test task over HTTP.
7. Response is SDK-compatible or adapter-compatible.
8. Response includes task linkage/state.
9. Response includes structured output and/or artifacts.
10. The resulting `DiscoveredAgent` can be used by the PI path.

## Output

Print a structured validation report:

```text
card_valid: true/false
oasf_conversion: true/false
endpoint_reachable: true/false
a2a_task_sent: true/false
a2a_task_state: completed | failed | working | ...
artifacts_count
structured_output_present
warnings
errors
```

Exit nonzero on hard failures.

## Tests

Add tests using at least one mock A2A endpoint.

Do not require real external agents for the default test suite.

---

# Section 4: Real-Agent Integration Guide

## Goal

Add documentation that tells other teams exactly how to replace a mock agent with their real agent.

## File

Create:

```text
v2_agent_network/docs/REAL_AGENT_INTEGRATION.md
```

## Required Contents

Include these sections:

1. What this repo expects from a real agent.
2. Required Agent Card fields.
3. Required OASF / ADS publication fields.
4. Skill naming policy.
5. Custom skill tag policy.
6. Required A2A endpoint behavior.
7. Expected task request shape.
8. Expected task result/artifact shape.
9. How to publish the real agent.
10. How to validate the real endpoint.
11. How to run PI against the real endpoint.
12. How to keep mock agents as regression tests.
13. Troubleshooting checklist.

## Required Agent Card Fields

Document at least:

```text
agent_id
name
organization
description
version
skills
domains
input_modes
output_modes
endpoint / locator
transport
auth requirements
required scopes
risk level
read/write classification
examples
```

## Skill Mapping Guidance

Explain that demo skills are currently custom tags unless official OASF taxonomy entries are known locally.

Examples:

```text
weather/forecast
calendar/search_availability
email/draft
image/geospatial_analysis
intelligence/situation_summary
code/repository_inspection
```

Do not pretend custom demo tags are official OASF skills.

## Agent-Specific Checklists

Include checklists for:

### Email Agent

Required skills:

```text
email/search
email/read
email/draft
email/send
email/summarize_thread
```

`email/send` must require confirmation.

### Calendar Agent

Required skills:

```text
calendar/search_availability
calendar/create_event
calendar/update_event
calendar/respond_invitation
calendar/summarize_schedule
```

Calendar mutations must require confirmation.

### Weather Agent

Required skills:

```text
weather/current_conditions
weather/forecast
weather/alerts
weather/route_weather_assessment
```

### Photo/Geospatial Agent

Required skills:

```text
image/geospatial_analysis
image/object_detection
image/location_inference
image/satellite_interpretation
image/photo_forensics_basic
```

Inputs should use file/artifact references, not large inline images.

### Intelligence Agent

Required skills:

```text
intelligence/situation_summary
intelligence/risk_assessment
intelligence/course_of_action
intelligence/entity_tracking
intelligence/report_generation
```

Should accept prior artifact references or structured outputs.

### Coding Agent

Required skills:

```text
code/repository_inspection
code/bug_fix
code/pr_review
code/test_generation
code/documentation
```

Repo modifications, shell commands, commits, or file writes must require confirmation.

---

# Section 5: Seventh-Agent Acceptance Test

## Goal

Prove that a new agent can be added by card + endpoint + Directory publication without editing PI endpoint code or hardcoding service URLs.

Use a test-only mock Logistics Agent.

## Test Agent

Create a temporary/test fixture agent:

```text
Name: Logistics Agent
Skill: logistics/route_planning
```

This agent does not need to be part of the main six-agent demo.

## Generic Capability-Driven PI Path

The current deterministic router is skill-specific. To support a new arbitrary agent without router edits for every new agent, add one generic routing path.

Choose one of these options:

### Option A: requested_skill field

Allow `POST /messages` to include:

```json
{
  "sender_id": "test-user",
  "text": "Plan a supply route from base to checkpoint.",
  "requested_skill": "logistics/route_planning"
}
```

### Option B: explicit skill syntax

Allow user text like:

```text
Use skill logistics/route_planning: Plan a supply route from base to checkpoint.
```

Prefer Option A if cleaner.

## Acceptance Test Steps

The test must:

1. Create or load a Logistics Agent Card.
2. Start/register its mock A2A server or ASGI app.
3. Publish its card/record through `DirectoryClient`.
4. Send PI a request using `requested_skill` or explicit skill syntax.
5. Verify PI discovers Logistics Agent through `DirectoryClient`.
6. Verify PI invokes Logistics Agent through `A2AClient` over HTTP.
7. Verify no PI endpoint URL or direct mock import is hardcoded.
8. Verify no change is required to PI endpoint/routing code for that specific agent beyond the generic capability path.

## Tests

Add:

```text
v2_agent_network/tests/test_milestone4a_seventh_agent.py
```

or similar.

---

# Section 6: External HTTP Mode

## Goal

Make sure `A2AClient` can target real external URLs, not only in-process ASGI transports.

## Requirements

Keep existing in-process ASGITransport tests.

Add a path where:

```text
DiscoveredAgent.endpoint = "http://..."
```

and `HTTPA2ATransport` can call it without requiring an ASGI app injection.

This can be covered by:

- an optional integration script, or
- a test marked as integration/skipped unless an endpoint is configured, or
- a local background Uvicorn server if lightweight and stable.

Do not make the default unit test suite depend on a real external process unless clearly marked optional.

## Script Option

Add optional CLI support in `validate_agent_endpoint.py` for testing a separately running A2A server URL.

Example:

```bash
v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card path/to/weather_agent.card.json \
  --skill weather/forecast \
  --prompt "What is the weather tomorrow near Austin?" \
  --endpoint http://localhost:9001
```

---

# Section 7: README Updates

## File

Update:

```text
v2_agent_network/README.md
```

## Required Sections

Add/update sections for:

1. Milestone 4A overview.
2. Fake ADS vs real ADS.
3. How to publish sample agents to fake ADS.
4. How to publish sample agents to real ADS.
5. How to validate a real external agent endpoint.
6. How to add a seventh agent without editing PI endpoint code.
7. How to replace a mock agent with a real agent.
8. All existing demo commands:
   - Weather
   - Calendar + Email
   - Geo + Weather + Intelligence
   - Coding
9. Known limitations.
10. Future milestones.

Correct any outdated line that says later milestones will add more mock agents, because the six main mock agents already exist after Milestone 3C.

## README Commands

Include:

```bash
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents --dry-run
```

```bash
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.publish_agents
```

```bash
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_weather_demo
```

```bash
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_calendar_email_demo
```

```bash
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_geo_weather_intelligence_demo
```

```bash
AGENT_NETWORK_FAKE_ADS=true v2_agent_network/.venv/bin/python -m v2_agent_network.scripts.run_coding_demo
```

---

# Section 8: Tests Required

Add or update tests for:

1. `publish_agents.py --dry-run` validates all six cards.
2. `publish_agents.py` publishes all six cards to fake ADS.
3. Real ADS healthcheck test is skipped when ADS is unavailable.
4. DirectoryClient real backend fails loudly when ADS unavailable and fake mode is false.
5. `validate_agent_endpoint.py` validates at least one mock A2A endpoint.
6. Seventh-agent acceptance:
   - dynamic Logistics Agent card
   - publish through DirectoryClient
   - PI discovers by `requested_skill` or explicit skill syntax
   - PI calls via A2AClient
   - no hardcoded endpoint
7. Existing Weather tests still pass.
8. Existing Calendar+Email tests still pass.
9. Existing Geo+Weather+Intelligence tests still pass.
10. Existing Coding tests still pass.

## Test Command

Use:

```bash
env PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  v2_agent_network/.venv/bin/python -m pytest v2_agent_network/tests -q -p no:cacheprovider
```

---

# Implementation Checkpoints

Do not implement all of 4A as one uncontrolled pass. Use checkpoints.

## Checkpoint 1: Directory + Publish Script

Implement only:

- Real/fake DirectoryClient separation.
- Real ADS backend scaffold or implementation.
- `publish_agents.py`.
- Fake ADS publish tests.
- Real ADS unavailable/fail-loud tests.
- README update for fake vs real ADS.

Stop and report:

- changed files
- tests run
- tests passed/failed/skipped
- remaining gaps

## Checkpoint 2: Endpoint Validation + Integration Guide

Implement only:

- `validate_agent_endpoint.py`.
- External HTTP A2A endpoint validation path.
- `docs/REAL_AGENT_INTEGRATION.md`.
- Tests using mock A2A endpoint.

Stop and report:

- changed files
- tests run
- tests passed/failed/skipped
- remaining gaps

## Checkpoint 3: Seventh-Agent Acceptance

Implement only:

- Generic `requested_skill` or explicit skill syntax path.
- Test-only Logistics Agent fixture.
- Seventh-agent acceptance test.
- README instructions for adding a new agent without PI endpoint-code changes.

Stop and report final 4A status.

---

# Codex Initial Prompt

Use this prompt first:

```text
Read AGENTS.md and v2_agent_network/docs/MILESTONE_4A_REAL_ADS_ONBOARDING.md.

Do not edit files yet.

Produce a Milestone 4A implementation plan with:
1. exact files to change
2. exact tests to add/update
3. dependency/API risks around agntcy-dir and ADS
4. how you will avoid breaking existing Weather, Calendar+Email, Geo+Weather+Intelligence, and Coding demos
5. how you will stage the work into checkpoints

Stop after the plan.
```

After reviewing the plan, proceed checkpoint by checkpoint.

# Codex Checkpoint 1 Prompt

```text
Implement Milestone 4A Checkpoint 1 only.

Use v2_agent_network/docs/MILESTONE_4A_REAL_ADS_ONBOARDING.md as the spec.

Implement:
- Real/fake DirectoryClient separation.
- Real ADS backend scaffold or implementation.
- publish_agents.py.
- fake ADS publish tests.
- real ADS unavailable/fail-loud tests.
- README update for fake vs real ADS.

Do not implement validate_agent_endpoint.py, REAL_AGENT_INTEGRATION.md, seventh-agent acceptance, Telegram, WhatsApp, Docker Compose, observability, persistent database state, or real SLIM/NATS.

Keep the old image-classification prototype out of this milestone's runtime path.
Keep all existing Weather, Calendar+Email, Geo+Weather+Intelligence, and Coding tests/demos working.

Stop after Checkpoint 1 and report changed files, tests, failures/skips, and remaining gaps.
```

# Codex Checkpoint 2 Prompt

```text
Implement Milestone 4A Checkpoint 2 only.

Use v2_agent_network/docs/MILESTONE_4A_REAL_ADS_ONBOARDING.md as the spec.

Implement:
- validate_agent_endpoint.py.
- external HTTP A2A endpoint validation path.
- docs/REAL_AGENT_INTEGRATION.md.
- tests using at least one mock A2A endpoint.

Do not implement seventh-agent acceptance, Telegram, WhatsApp, Docker Compose, observability, persistent database state, or real SLIM/NATS.

Keep the old image-classification prototype out of this milestone's runtime path.
Keep all existing tests/demos working.

Stop after Checkpoint 2 and report changed files, tests, failures/skips, and remaining gaps.
```

# Codex Checkpoint 3 Prompt

```text
Implement Milestone 4A Checkpoint 3 only.

Use v2_agent_network/docs/MILESTONE_4A_REAL_ADS_ONBOARDING.md as the spec.

Implement:
- generic requested_skill or explicit skill syntax path.
- test-only Logistics Agent fixture.
- seventh-agent acceptance test.
- README instructions for adding a new agent without PI endpoint-code changes.

Do not implement Telegram, WhatsApp, Docker Compose, observability, persistent database state, or real SLIM/NATS.

Keep the old image-classification prototype out of this milestone's runtime path.
Keep all existing tests/demos working.

Stop after Checkpoint 3 and report final 4A status.
```

---

# Acceptance Bar for Milestone 4A

Accept 4A only if:

1. All existing demos still work:
   - Weather
   - Calendar + Email
   - Geo + Weather + Intelligence
   - Coding
2. All existing v2 tests still pass.
3. `publish_agents.py` can dry-run all six sample cards.
4. `publish_agents.py` can publish all six sample cards to fake ADS.
5. `DirectoryClient` does not silently use fake ADS when fake mode is false.
6. Real ADS path exists and either works or fails loudly with clear config errors.
7. `validate_agent_endpoint.py` can validate a mock A2A endpoint.
8. A new seventh agent can be published and called without hardcoding its URL in PI endpoint code.
9. README explains how another team can replace a mock with a real agent.
10. `REAL_AGENT_INTEGRATION.md` gives actionable instructions for real agent teams.

---

# Final Report Required From Codex

After completing 4A, Codex must report:

```text
Changed files
Tests run
Tests passed/failed/skipped
Command to publish sample agents to fake ADS
Command to dry-run publication
Command to validate one mock agent endpoint
Command to run seventh-agent acceptance test/demo
How to publish to real ADS
What requires real ADS or real agent endpoints to validate later
Remaining gaps before Milestone 4B
```
