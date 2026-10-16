# Testing

The unit and pipeline suites run offline: no Docker, Postgres, real `dirctl`, external
services or API keys. Run them from the repo root. CI
([`.github/workflows/tests.yml`](../.github/workflows/tests.yml)) runs the same suites on every
pull request, on pushes to `main` and on demand. A second workflow
([`.github/workflows/compose.yml`](../.github/workflows/compose.yml)) runs the tier-2 Docker check.

## Three tiers

The request path (user → planner → directory → A2A → agent → answer) and the registration path
(register → spawn → publish → discoverable) are checked at three levels of realism.

| Tier | Runs | What is real | What stands in |
|---|---|---|---|
| 1. Native | CI, every PR | The planner, the registration service and the agents it spawns, as local processes from your checkout, talking HTTP, A2A and the `dirctl` CLI | A mock LLM (`devstack/mock_llm.py`), in-memory databases, a file-backed fake `dirctl` (`devstack/fake_dirctl.py`), a local identity proxy instead of SSO |
| 2. Containers | CI (`compose.yml`): PRs and pushes to `main` touching deployment or service code, weekly, on demand; or by hand | Tier 1 plus the service images, Postgres and a real directory daemon | The mock LLM (by default) and the identity proxy |
| 3. Real LLM | By hand, opt-in | Tier 1 with a hosted or local model | Everything else as in tier 1 |

- **Tier 1** is the unit suites and the pipeline suite below.
- **Tier 2** is the Docker Compose stack ([`deployment/COMPOSE.md`](deployment/COMPOSE.md)), checked by `deployment/compose/smoke.sh`.
- **Tier 3** has no automated test yet. Set a model as in
  [DEVSTACK.md, "Switching the LLM"](DEVSTACK.md#switching-the-llm), then run
  `scripts/stack up --llm openai` and `scripts/stack smoke`: one prompt through the front door,
  checking that a specialist answered and that the conversation was saved. A hosted model
  charges per call; a local one (for example Ollama) does not.

## The suites

| Suite | Command | Tests | Line coverage | What it covers |
|---|---|---|---|---|
| Planner | `planner_agent/.venv/bin/python -m pytest planner_agent/tests -q` | 143 | 63% | The tool loop (scripted LLM, fake directory), the HTTP API (identity, conversation ownership, streaming, confirmations), credential discovery and enforcement, `/ready`, startup, bug reports |
| Registration | `registration_service/.venv/bin/python -m pytest registration_service/tests -q` | 301 | 80% | The API, upload checks, the image build manifest, the native supervisor and agent processes, the Cloud Run backend (mocked), streaming and tool-loop agents, credentials, the fake database |
| Agent skeleton | `registration_service/.venv/bin/python -m pytest agent_skeleton/tests -q` | 70 | 73% | The spec-driven engine, the `call_endpoint` tool and its secret redaction, per-operation tools, preset tools, the multi-endpoint manager, the generator, credentials kept out of the LLM prompt, the tool-schema and handler contract |
| Directory tooling | `registration_service/.venv/bin/python -m pytest agent_directory_service/tests -q` | 22 | 50% | Card ↔ record conversion, the `dirctl` CLI backend (mocked), route summaries and routing wake-up |
| Dev stack | `registration_service/.venv/bin/python -m pytest devstack/tests -q` | 55 | 52% | The mock LLM, the fake `dirctl`, the identity proxy, the stack CLI's checks that need no running services |
| Pipeline | `registration_service/.venv/bin/python -m pytest tests/pipeline -q` | 19 | — | The running system, end to end (next section) |

Counts and coverage were measured on 2026-10-15 with the local venvs; both move as tests are
added. Line coverage counts the component's own code, its tests left out. It sees only code that
runs inside the pytest process, so code a suite starts in a subprocess shows as missed: the stack
CLI's `up`, the fake `dirctl` behind `devstack/bin/dirctl`, and agent processes. The pipeline
suite exercises much of that code without measuring it.

The registration venv runs four suites because it matches the directory tooling's `a2a-sdk` pin
and installs `agent_skeleton`. Do not set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`: the registration
suite needs the `pytest-asyncio` plugin, and coverage comes from the `pytest-cov` plugin.

### Coverage

CI prints a coverage table for each unit suite (report only, no threshold). Locally, with
`pytest-cov` installed in the venv:

```bash
planner_agent/.venv/bin/python -m pip install pytest-cov
planner_agent/.venv/bin/python -m pytest planner_agent/tests -q --cov=planner_agent --cov-report=
planner_agent/.venv/bin/python -m coverage report --omit='*/tests/*'
```

Swap in the other component and venv for the other suites. The data file `.coverage` is ignored
by git.

## The pipeline suite

`tests/pipeline` starts the dev stack with `scripts/stack up --llm mock` and drives it only through
`scripts/stack`, HTTP and the dev stack's fake `dirctl`. Being black box, it can also judge a
component rebuilt from scratch. It needs both service venvs ([DEVSTACK.md](DEVSTACK.md)) and a
Python 3.11 or newer with pytest; it takes about 30 seconds.

| Variable | Default | Meaning |
|---|---|---|
| `PIPELINE_PORT_OFFSET` | `20000` | Added to every stack port, so the suite can run next to your own dev stack |
| `PIPELINE_STATE_DIR` | `.devstack-pipeline/` | The stack's state dir. Reset at the start of each file; its `logs/` are kept afterwards |

Each file starts a stack of its own in the same state dir, so a run of the whole folder keeps
only the second file's logs. To debug a failure, run that file alone, then read
`<state dir>/logs/` or `scripts/stack logs SERVICE --state-dir <state dir>`. CI runs the two files
as separate steps with separate state dirs and uploads both `logs/` folders when a step fails.

**`test_full_chain.py`** (8 tests) is one first-time user's session, in order: start from nothing
(`up` is ready and idempotent); the first signed-in request creates the user, and identity
headers from the browser are dropped; a prompt sent the chat UI's way reaches the seed agent over
A2A and comes back; the conversation is saved for that user only; the stream sends each step,
then one final event; an agent registered with `add-agent` receives the prompt that matches it; a
`user_id` in the request body is ignored when a login identity is present; `down` leaves no
process or port behind.

**`test_scenarios.py`** (11 tests) runs on a stack whose planner gives up on an agent after
5 seconds (`A2A_HTTP_TIMEOUT_SECONDS`; the default is 30), stops a turn after 6 tool steps
(`MAIN_AGENT_MAX_STEPS`; the default is 10) and asks before a write skill
(`MAIN_AGENT_POLICY_MODE=strict`), all passed through with `DEVSTACK_PASSTHROUGH_ENV`. Agents
added with `add-agent` have their saved definitions removed after each test. Agents the tests run themselves are published into the stack's directory with the
fake `dirctl` (`push`, then `routing publish`) and unpublished afterwards.

- **Credential scoping.** From one user's credential bundle, an agent whose card declares
  `openai_api_key` receives only that type, and an agent that declares nothing receives no
  credentials. A small A2A server inside the test records what each agent was sent.
- **Unreachable agent.** An agent that refuses the connection, or accepts it and never answers,
  ends the turn with a failed `call_agent` that names the agent, and the reply reaches the user:
  at once for the refused connection, within the timeout for the silent one. No hang, no stream
  error.
- **Proxy agent.** A `proxy` agent registered with `add-agent` (the real `POST /agents` path)
  relays the planner's task to its upstream (the seed agent): one upstream LLM call, whose reply
  is the answer.
- **Preset tool.** An `llm` agent with the preset `calculator` calls it with the arguments the
  prompt gives and answers with the result; `tools_used` names it.
- **Step limit.** A turn whose model never answers stops after 6 steps with `input_required`
  and `max_steps_exceeded`, and the stream ends with one final event.
- **ask_user.** The planner's question pauses the turn (`input_required`); the reply in the same
  conversation reaches the agent with the question and answer in its history.
- **Confirmation.** A write skill waits for confirmation. Another user's confirm or cancel gets
  403, and another user's chat "yes" or "no" naming the conversation is refused; the owner's
  confirm runs it once with `confirmed` in its metadata; a second confirm fails; a chat "no"
  cancels it without running it. Two confirms sent together, with the agent slowed so the
  second arrives during the first one's call, run it once, and the other fails.
- **Restart.** After `down` and `up` on the same state dir, an agent added with `add-agent` is
  listed once, by the same id, and answers its prompt.

### What the pipeline suite does not prove

- **Model behaviour.** The mock LLM follows a script: list the agents, call the one whose name and
  description share the most words with the prompt, relay its answer. Directives in the prompt
  and tool names in an agent's message drive the other scenarios ([DEVSTACK.md](DEVSTACK.md)).
  A real model's tool choice, wording and prompt handling are untested (tier 3).
- **The real directory.** The fake `dirctl` keeps records in one JSON file. It models "stored is not
  routed" (a record is discoverable only after `routing publish`), but not the daemon, its
  network routing or its timing.
- **Persistence and Postgres.** Registration runs with `FAKE_DB=1` and the planner without a
  database: users and conversations live in memory, no Postgres constraints apply, and the
  planner's credential store, grants and consent prompts are switched off. So the credential test
  sends the bundle as the request's `metadata.credentials`, the key under which the planner's
  server hands stored credentials to the runtime. It proves the scoping; storing, granting and
  consent need tier 2. The restart test proves the stack's own restart path: the registry is in
  memory, and `up` registers the seeds and the agents in `<state dir>/agents/` again. A
  confirmation or `ask_user` resumed on another planner process needs tier 2.
- **Login.** The identity proxy adds `mail: dev@example.test`; Shibboleth and the ENGR gateway
  are not involved.
- **Deployments.** Agents run as local processes, not images. The ENGR cluster scripts and Cloud
  Run are not exercised; the Cloud Run backend has only mocked unit tests.

## Adding a test

- **Unit test:** put it in the component's `tests/` folder and run it with that component's venv.
  Keep it offline and use the fakes already there: the planner's `ScriptedLLMClient`
  (`planner_agent/main_agent/llm_client.py`), `DirectoryClient(fake_ads=True)` and the cards in
  `planner_agent/tests/fixtures/agent_cards/`; registration's `FAKE_DB=1`
  (`registration_service/tests/conftest.py` sets it).
- **Pipeline test:** only if the behaviour needs the running system. Drive it through
  `scripts/stack`, HTTP and the fake `dirctl`, never by importing service code. A step of a
  first-time user's session goes in `test_full_chain.py`; anything else in `test_scenarios.py`,
  reusing the helpers in `test_full_chain.py` (`stack_cli`, `stack_json`, `call`, `stream`,
  `chat`, `assert_delegated`, `fresh_stack`). Clean up what you publish (the `directory`
  fixture does). Keep `test_scenarios.py` around 25 seconds or less.
- **Determinism:** give a new agent words no other agent has, so the mock planner picks it; poll
  with a deadline instead of sleeping for a fixed time; take ports from `scripts/stack status
  --json` or let the OS pick one (port 0), never a fixed number.
- **Security fixes** get a regression test in the same change.

## Flaky tests

- A test that fails without a code change is a bug, in the test or in the system. Fix it or
  remove it; do not add automatic retries.
- Before merging a new or changed pipeline test, run the suite several times in a row on your
  machine, for example five:

  ```bash
  for i in 1 2 3 4 5; do registration_service/.venv/bin/python -m pytest tests/pipeline -q -p no:cacheprovider || break; done
  ```

  The full-chain test passed 20 runs in a row on 2026-10-13; the suite with all eleven scenario
  tests passed 5 in a row on 2026-10-15 (about 32 seconds each).
- A `ConnectTimeout` on the first chat after a restart was traced to the planner leaving its LLM
  clients for the garbage collector to close at an arbitrary moment. It showed up when the
  restarted stack registered more agents than the restart test's own. It is fixed (one client per
  endpoint, closed on shutdown) and guarded by `planner_agent/tests/test_llm_client.py`; a
  recurrence is a regression, not load.
- A security scenario that fails today goes in as a strict expected failure,
  `@pytest.mark.xfail(strict=True, reason="...")`: the suite stays green while the bug exists and
  fails as soon as the fix lands, so the marker is removed with the fix. None are open now.
