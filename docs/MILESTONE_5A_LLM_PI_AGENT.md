# Milestone 5A: LLM-Powered PI Agent over Real ADS

## Purpose

Implement the PI Agent as an LLM-powered coordinator.

The PI Agent should receive natural-language user messages, use an LLM to convert the message into a structured skill/workflow plan, discover agents from **real ADS**, apply policy, and invoke selected agents through **A2A**.

This milestone is **not** about Telegram, WhatsApp, more mock agents, or confirmed write actions.

## Current Baseline

Milestone 4C is accepted.

Current working proof:

```text
Live Email Agent
  -> published to real ADS through dirctl
  -> PI discovers Email Agent from real ADS
  -> PI invokes Email Agent through A2A
  -> safe skill email/current_time completes
```

Known safe live Email Agent endpoint:

```text
http://10.229.117.223:9103/
```

Known safe skill for live tests:

```text
email/current_time
```

Dangerous/write skills must remain blocked before A2A invocation:

```text
email/send
email/calendar/create_event
email/docs/create
email/docs/append
email/docs/replace_text
email/docs/create_and_email
email/sheets/create
email/sheets/write
email/sheets/append
code/bug_fix
code/test_generation
```

## Final Product Behavior

The final behavior should be:

```text
User text
  -> PI Agent server
  -> PI LLM planner
  -> structured workflow of skill names
  -> DirectoryClient searches real ADS
  -> PI selects discovered agent
  -> policy.py decides allowed / confirmation_required / denied
  -> A2AClient invokes allowed agents only
  -> PI formats response
```

The LLM is only the **planner and summarizer**.

The LLM must never:

- choose a raw endpoint URL;
- bypass ADS;
- bypass `DirectoryClient`;
- bypass `policy.py`;
- approve write actions;
- directly call agents;
- directly call `A2AClient`;
- send email, create calendar events, create Docs, or write Sheets.

## Required Protocol Boundary

Keep this boundary strict:

```text
ADS / AGNTCY Directory
  = publish and discover agents

A2A
  = runtime task/message/artifact protocol

PI LLM
  = convert user language into skill/workflow plan

policy.py
  = authority for allowed vs confirmation_required vs denied
```

Do not use ADS as task transport.
Do not use A2A Agent Card well-known URLs as the global registry.
Do not use fake ADS in the main Milestone 5A demo.

## Configuration

Add or support these environment variables:

```bash
# Required only when PI_LLM_ENABLED=true
OPENAI_API_KEY=...

# LLM planner config
PI_LLM_ENABLED=true
PI_LLM_PROVIDER=openai
PI_LLM_MODEL=gpt-5.5
PI_LLM_REASONING_EFFORT=low
PI_LLM_VERBOSITY=low
PI_LLM_MAX_OUTPUT_TOKENS=1200
PI_LLM_TIMEOUT_SECONDS=30
PI_LLM_ALLOW_DETERMINISTIC_FALLBACK=false
PI_LLM_USE_FOR_FINAL_SUMMARY=true

# Real ADS config
AGENT_NETWORK_FAKE_ADS=false
ADS_BACKEND=cli
ADS_SERVER_ADDR=localhost:8888

# Optional live integration testing
LIVE_EMAIL_AGENT_URL=http://10.229.117.223:9103/
RUN_LIVE_OPENAI_TESTS=false
RUN_REAL_ADS_TESTS=false
```

Rules:

- Do not put `OPENAI_API_KEY` in frontend code.
- Do not commit API keys.
- If `PI_LLM_ENABLED=true` and `OPENAI_API_KEY` is missing, fail loudly with a clear config error.
- Deterministic fallback is allowed only when `PI_LLM_ALLOW_DETERMINISTIC_FALLBACK=true`.
- If `PI_LLM_ENABLED=false`, keep the existing deterministic router behavior.

## Files To Add

Add:

```text
v2_agent_network/services/pi_agent/llm_config.py
v2_agent_network/services/pi_agent/llm_planner.py
v2_agent_network/services/pi_agent/skill_catalog.py
v2_agent_network/tests/test_pi_llm_config.py
v2_agent_network/tests/test_pi_llm_planner.py
v2_agent_network/tests/test_pi_llm_policy_safety.py
```

Optional if needed:

```text
v2_agent_network/services/pi_agent/response_summarizer.py
v2_agent_network/scripts/run_llm_pi_email_demo.py
```

## Files To Update

Update:

```text
v2_agent_network/services/pi_agent/main.py
v2_agent_network/services/pi_agent/router.py
v2_agent_network/services/pi_agent/policy.py   # only if policy coverage gaps are found
v2_agent_network/scripts/run_pi_agent_server.py
v2_agent_network/README.md
v2_agent_network/docs/REAL_AGENT_INTEGRATION.md
```

Do not edit:

```text
v1_classification/
external_agents/email_agent/email_agent.py
```

Do not change dependency files unless explicitly approved.

## LLM Planner Output Contract

The LLM planner must return strict JSON matching this shape:

```json
{
  "workflow": [
    {
      "skill": "email/current_time",
      "reason": "The user asks for the Email Agent's current local time.",
      "confidence": 0.95,
      "risk_hint": "read",
      "requires_confirmation": false,
      "required_inputs": {}
    }
  ],
  "needs_clarification": false,
  "clarifying_question": null,
  "user_visible_plan": "I will ask the Email Agent for its current local time."
}
```

Allowed top-level fields:

```text
workflow
needs_clarification
clarifying_question
user_visible_plan
```

Allowed workflow step fields:

```text
skill
reason
confidence
risk_hint
requires_confirmation
required_inputs
```

The LLM output must not contain:

```text
endpoint
url
agent_url
base_url
raw HTTP destination
credentials
api_key
```

If the LLM returns any endpoint-like field or raw URL, reject the plan.

## Implementation Details

### 1. `llm_config.py`

Implement a config loader:

```python
class PILLMConfig(BaseModel):
    enabled: bool
    provider: str
    model: str
    reasoning_effort: str | None
    verbosity: str | None
    max_output_tokens: int
    timeout_seconds: float
    allow_deterministic_fallback: bool
    use_for_final_summary: bool
```

Behavior:

- Read from environment variables.
- If enabled and provider is `openai`, require `OPENAI_API_KEY`.
- Return clear error messages.
- Do not print or log the API key.

### 2. `skill_catalog.py`

Implement a capability catalog for the LLM planner.

Goal:

```text
The LLM should choose from published/discovered skills, not from hardcoded endpoint URLs.
```

Implement:

```python
class AvailableSkill(BaseModel):
    skill: str
    agent_id: str
    agent_name: str
    description: str
    risk_level: str
    action_type: str
    input_modes: list[str]
    output_modes: list[str]
    examples: list[str]
```

Add function:

```python
def build_skill_catalog(directory_client: DirectoryClient) -> list[AvailableSkill]
```

Real ADS behavior:

- Use `DirectoryClient` / ADS records.
- Do not use `PI_AGENT_CARD_PATHS` in real ADS mode.
- Extract skills from pulled A2A Agent Card `card_data` and custom skill tags.
- If real ADS cannot globally list all capabilities, implement the best available real ADS enumeration using existing CLI/routing list/search behavior.
- If enumeration is incomplete, fail loudly or return a clearly marked partial catalog; do not silently fall back to fake ADS.

Fake ADS behavior:

- Allowed only in tests or demos explicitly setting `AGENT_NETWORK_FAKE_ADS=true`.

### 3. `llm_planner.py`

Implement:

```python
class PIPlannerPlan(BaseModel):
    workflow: list[PIPlannerStep]
    needs_clarification: bool
    clarifying_question: str | None
    user_visible_plan: str

class PIPlannerStep(BaseModel):
    skill: str
    reason: str
    confidence: float
    risk_hint: str
    requires_confirmation: bool
    required_inputs: dict[str, Any]
```

Implement:

```python
class PILLMPlanner:
    def __init__(self, config: PILLMConfig, openai_client: Any | None = None): ...

    async def plan(
        self,
        *,
        user_text: str,
        requested_skill: str | None,
        available_skills: list[AvailableSkill],
        conversation_context: dict[str, Any] | None = None,
    ) -> PIPlannerPlan: ...
```

Use OpenAI Responses API.

Use Structured Outputs / strict JSON schema if supported by the installed OpenAI SDK. If the installed SDK does not support structured output helpers, implement strict JSON parsing plus Pydantic validation.

Planner instructions:

- Choose only from `available_skills` unless `requested_skill` is present.
- If `requested_skill` is present, validate that it appears in available skills.
- Return a workflow of skill IDs, not endpoints.
- If no suitable skill exists, return `needs_clarification=true` or an empty workflow.
- Mark write-risk skills as requiring confirmation, but remember policy.py is the authority.
- Never approve write actions.

### 4. Update `main.py`

Current `POST /messages` flow should become:

```text
receive UserMessage
  -> build or load context
  -> build skill catalog from DirectoryClient
  -> if PI_LLM_ENABLED=true:
       call PILLMPlanner.plan(...)
     else:
       use existing deterministic router
  -> validate planned skills
  -> for each skill:
       DirectoryClient.search_agents(skill)
       select discovered agent
       policy.evaluate_skill(skill, capability)
       if confirmation_required:
           record pending confirmation
           do NOT invoke A2A
       if allowed:
           invoke A2AClient
       if denied:
           return denied
  -> optionally summarize final result
  -> return structured response
```

Response must include:

```text
context_id
planner_mode: llm | deterministic
llm_model, if enabled
workflow
selected_agents
policy_decisions
confirmation_required
task_ids
task_states
artifacts
structured_outputs
final_response
errors
```

### 5. Keep deterministic router

Do not delete the existing deterministic router.

Use it when:

```text
PI_LLM_ENABLED=false
```

or when:

```text
PI_LLM_ENABLED=true
PI_LLM_ALLOW_DETERMINISTIC_FALLBACK=true
LLM planner fails
```

If fallback is disabled and the LLM fails, return a clear error.

### 6. Safety requirements

Hard safety gates live outside the LLM.

The following must remain true:

```text
requested_skill=email/send
  -> confirmation_required
  -> A2A not invoked
  -> no email sent

LLM plans email/send
  -> confirmation_required
  -> A2A not invoked
  -> no email sent

LLM outputs endpoint URL
  -> plan rejected
```

Do not run automated tests that invoke:

```text
email/send
email/calendar/create_event
email/docs/create
email/docs/append
email/docs/replace_text
email/docs/create_and_email
email/sheets/create
email/sheets/write
email/sheets/append
```

Use safe live skills only:

```text
email/current_time
email/google/status
email/draft with create_gmail_draft=false
```

## Tests

Add unit tests with a mocked OpenAI client. Do not require live OpenAI by default.

Required tests:

1. `PI_LLM_ENABLED=false` keeps deterministic routing working.
2. `PI_LLM_ENABLED=true` with missing `OPENAI_API_KEY` fails clearly.
3. LLM planner parses valid strict JSON into `PIPlannerPlan`.
4. LLM planner rejects outputs containing endpoint URLs or endpoint-like fields.
5. Mocked LLM maps:

```text
"What time is it for the email agent?" -> email/current_time
```

6. Mocked LLM maps:

```text
"Send an email to Bob saying hello" -> email/send
```

but PI policy returns `confirmation_required` and does not invoke A2A.

7. LLM-planned skill still goes through `DirectoryClient.search_agents`.
8. LLM-planned skill still goes through `policy.evaluate_skill`.
9. LLM cannot select an agent that ADS did not discover.
10. Real ADS mode does not use `PI_AGENT_CARD_PATHS`.
11. Existing deterministic tests still pass.
12. Full v2 suite still passes.

Optional live tests, skipped unless explicitly enabled:

```text
RUN_LIVE_OPENAI_TESTS=true
RUN_REAL_ADS_TESTS=true
LIVE_EMAIL_AGENT_URL=http://10.229.117.223:9103/
```

Optional live test behavior:

- Start `dirctl daemon`.
- Publish the live Email Agent card to real ADS.
- Use live OpenAI planner to plan `email/current_time`.
- PI discovers Email Agent from real ADS.
- PI invokes A2A safe read-only skill.
- Confirm no write skill was invoked.

## Safe Demo Commands

### Terminal 1: start real ADS

```bash
dirctl daemon start
```

### Terminal 2: publish live Email Agent to real ADS

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=false \
ADS_BACKEND=cli \
ADS_SERVER_ADDR=localhost:8888 \
v1_classification/.venv/bin/python -m v2_agent_network.scripts.publish_agents \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json
```

### Terminal 3: run PI server with LLM planner

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
OPENAI_API_KEY=<your-key> \
PI_LLM_ENABLED=true \
PI_LLM_PROVIDER=openai \
PI_LLM_MODEL=gpt-5.5 \
PI_LLM_REASONING_EFFORT=low \
PI_LLM_VERBOSITY=low \
AGENT_NETWORK_FAKE_ADS=false \
ADS_BACKEND=cli \
ADS_SERVER_ADDR=localhost:8888 \
v1_classification/.venv/bin/python -m v2_agent_network.scripts.run_pi_agent_server \
  --host 0.0.0.0 \
  --port 8080
```

### Terminal 4: send safe request

```bash
curl -X POST http://localhost:8080/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo-user",
    "channel": "web",
    "text": "What time is it for the email agent?"
  }'
```

Expected:

```text
planner_mode = llm
workflow includes email/current_time
PI searches real ADS
PI discovers Email Agent
policy allowed
A2A invoked
state completed
```

### Safety test: send is blocked

```bash
curl -X POST http://localhost:8080/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo-user",
    "channel": "web",
    "text": "Send an email to Bob saying hello."
  }'
```

Expected:

```text
workflow includes email/send
confirmation_required = true
A2A not invoked
no email sent
```

## Documentation Updates

Update README and REAL_AGENT_INTEGRATION.md.

Document:

- PI Agent is now optionally LLM-powered.
- LLM planner is enabled by `PI_LLM_ENABLED=true`.
- `OPENAI_API_KEY` must be set on the server, never in frontend code.
- LLM output is a skill/workflow plan, not an endpoint selection.
- ADS discovery remains mandatory.
- policy.py remains the authority for write actions.
- Real ADS + live Email Agent demo commands.
- Telegram/WhatsApp are no longer the next UI plan; web frontend comes after this milestone.

## Stop Report

After implementation, stop and report:

```text
changed files
tests run
pass/fail/skip counts
whether live OpenAI was contacted
whether live Email Agent was contacted
whether real ADS was contacted
whether any write skill was invoked
safe demo command
blocked-send safety command
confirmation that v1_classification/ was untouched
confirmation that no dependency files changed
```

## Acceptance Criteria

Accept Milestone 5A only if:

1. PI can run with `PI_LLM_ENABLED=false` and old deterministic tests still pass.
2. PI can run with `PI_LLM_ENABLED=true` and mocked LLM tests pass.
3. LLM planner outputs strict validated workflow JSON.
4. LLM cannot output or use raw endpoints.
5. LLM-planned skills are discovered through real ADS.
6. LLM-planned skills are checked by policy.
7. `email/current_time` can be planned and invoked safely.
8. `email/send` is blocked before A2A invocation.
9. No write side effects happen in automated tests.
10. README clearly documents model/API-key configuration.
