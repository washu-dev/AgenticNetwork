# Testing

The unit and pipeline suites run offline: no Docker, Postgres, real `dirctl`, external
services or API keys. `scripts/stack test` runs them all from the repo root, with the dev venv
([Running the suites](#running-the-suites)). CI
([`.github/workflows/tests.yml`](../.github/workflows/tests.yml)) runs the same command on every
pull request, on pushes to `main` and on demand. A second workflow
([`.github/workflows/compose.yml`](../.github/workflows/compose.yml)) runs the tier-2 Docker check.

## Three tiers

The request path (user → planner → directory → A2A → agent → answer) and the registration path
(register → spawn → publish → discoverable) are checked at three levels of realism.

| Tier | Runs | What is real | What stands in |
|---|---|---|---|
| 1. Native | CI, every PR | The chat app, the registration service and the agents it spawns, as local processes from your checkout, talking HTTP, A2A and the `dirctl` CLI | A mock LLM (`devstack/mock_llm.py`), in-memory databases, a file-backed fake `dirctl` (`devstack/fake_dirctl.py`), a local identity proxy instead of SSO |
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
| Chat app | `scripts/stack test chat_app` | 260 (+12) | 76% | The tool loop (scripted LLM, fake directory), the HTTP API (identity, conversation ownership, streaming, confirmations), credential discovery and enforcement, `/ready`, startup, bug reports, setting names and their old-name fallbacks, that every import of the repo's own code resolves and the package's layers import one way |
| Registration | `scripts/stack test registration_service` | 485 | 87% | The API, upload checks, the image build manifest, the native supervisor and agent processes, the Cloud Run backend (mocked), which handler module the runner starts for each handler type, streaming and uploaded-tools agents, credentials, the fake database, that every import of the repo's own code (here and in `agent_skeleton/`) resolves |
| Agent skeleton | `scripts/stack test agent_skeleton` | 98 | 78% | The spec-driven engine, the `call_endpoint` tool and its secret redaction, per-operation tools, preset tools, the multi-endpoint manager, the generator, credentials kept out of the LLM prompt, the tool-schema and handler contract, the demo agent, and that the runtime (from the source tree and from a regular install) never loads the demo |
| agentnet (directory + A2A) | `scripts/stack test agentnet` | 87 | 61% | Card ↔ record conversion, the `dirctl` CLI backend (mocked), the A2A transport against local peers (the timeout on streaming calls to a silent or stalled agent, an error reply's own status), the registration service's publisher (id-only skills, no legacy routing, its errors), the fake-directory flag, route summaries and routing wake-up |
| Dev stack | `scripts/stack test devstack` | 82 | 61% | The mock LLM, the fake `dirctl`, the identity proxy, the stack CLI's checks that need no running services, `scripts/stack test` (with fake interpreters) |
| Pipeline | `scripts/stack test pipeline` (`full-chain`, then `scenarios`) | 20 | — | The running system, end to end (next section) |

Counts and coverage were measured for every suite on 2026-10-26 with the dev venv
(`scripts/stack test unit --parallel --cov`); both move as tests are added. One agent skeleton test (`test_demo.py`, the regular-install check)
builds the package offline and skips when the interpreter has no `setuptools` (a Python 3.12
venv has none unless you install it). The chat app's other 12 tests (`test_pending_store_postgres.py`) run the
pending-confirmation statements on a real Postgres and are skipped unless
`PLANNER_TEST_DATABASE_DSN` names a scratch database (each test drops and recreates
`pending_confirmations` there). One way to get one, from the repo's Postgres image
(`deployment/dockerfiles/Dockerfile.postgres`), built and started from the repo root (the
database must be UTF8, as the deployment's entrypoint makes it):

```bash
docker build -f deployment/dockerfiles/Dockerfile.postgres -t agenticnetwork-postgres:local .
docker run -d --rm --name pending-store-pg -p 127.0.0.1:55432:5432 --user postgres --entrypoint bash agenticnetwork-postgres:local -c 'B=/usr/lib/postgresql/15/bin; $B/initdb -D /tmp/pg -A trust --encoding=UTF8 --locale=C >/dev/null && echo "host all all 0.0.0.0/0 trust" >> /tmp/pg/pg_hba.conf && exec $B/postgres -D /tmp/pg -c listen_addresses="*" -c unix_socket_directories=/tmp'
```

then `docker exec pending-store-pg /usr/lib/postgresql/15/bin/createdb -h /tmp pending_test` and
run the suite with
`PLANNER_TEST_DATABASE_DSN=postgresql://postgres@127.0.0.1:55432/pending_test`. That variable is
separate from `DATABASE_URL`: every chat app test starts with `DATABASE_URL` and the old
`REGISTRATION_DB_DSN` cleared (`chat_app/tests/conftest.py`), so a value in your shell never puts
the suite in DB mode. CI has no Postgres, so run them before merging a change to `pending_store.py`. They passed on Postgres
15.19 on 2026-10-16. Line coverage counts the component's own code, its tests left out. It sees only code that
runs inside the pytest process, so code a suite starts in a subprocess shows as missed: the stack
CLI's `up`, the fake `dirctl` behind `devstack/bin/dirctl`, and agent processes. The pipeline
suite exercises much of that code without measuring it.

Every suite runs in the dev venv, `.venv` at the repo root, created from `requirements-dev.txt`:
both services' requirements (one `a2a-sdk` version, 0.3.2), `agent_skeleton` in editable mode and
`pytest-cov` ([DEVSTACK.md](DEVSTACK.md#one-time-setup-the-dev-venv)). agentnet runs inside both
services, on that one version, so its suite runs once. The registration suite needs the
`pytest-asyncio` plugin, and coverage comes from the `pytest-cov` plugin; when you call pytest
yourself, do not set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.

### Running the suites

```bash
scripts/stack test                     # everything: the unit suites, then the pipeline
scripts/stack test unit --parallel     # the unit suites at once
scripts/stack test agentnet devstack   # suites by name, or a group: unit, pipeline, all
scripts/stack test registration_service -- -k approval -x   # after --: pytest's own arguments
scripts/stack test --list              # what would run, with which interpreter
scripts/stack test --json              # pytest's output on stderr, a JSON summary on stdout
```

Each suite is its own `python -m pytest <path> -q -p no:cacheprovider` from the repo root
(`devstack/suites.py`), because the suites differ in rootdir and asyncio mode
(`registration_service/pytest.ini` applies only when the registration suite runs alone). Each
gets a clean environment: the dev stack's pass-through list (`devstack/stack.py`) plus
`PLANNER_TEST_DATABASE_DSN`, so a stray `FAKE_DB=0` or `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` in your
shell cannot change a result. Selected suites run in table order and the command carries on
after a failure; `--parallel` runs the unit suites at once, but the pipeline files always run one
after the other. A suite still running after `--timeout` seconds (default 900, 600 per pipeline
file) is stopped. `--python PATH` runs every suite with that interpreter instead of the dev
venv's, as CI does (on Python 3.11 and 3.13).

The command exits 0 when every selected suite passed, 7 when any failed or was stopped, 3 when the
dev venv, a test plugin or (for the pipeline) `lsof` is missing, naming the fix, and 2 for an unknown
suite.

### Coverage

CI prints a coverage table for each unit suite (report only, no threshold). Locally
(`requirements-dev.txt` installs `pytest-cov`):

```bash
scripts/stack test unit --cov
```

Each suite's data stays in `.coverage.<suite>` (for example `.coverage.agentnet`),
ignored by git; the table is `coverage report --omit='*/tests/*'` on that file.

## The pipeline suite

`tests/pipeline` starts the dev stack with `scripts/stack up --llm mock` and drives it only through
`scripts/stack`, HTTP and the dev stack's fake `dirctl`. Being black box, it can also judge a
component rebuilt from scratch. `scripts/stack test pipeline` runs it from the dev venv, which
the stack's services also run from ([DEVSTACK.md](DEVSTACK.md)); it needs `lsof` too, and takes about 30 seconds.

| Variable | Default | Meaning |
|---|---|---|
| `PIPELINE_PORT_OFFSET` | `20000` | Added to every stack port, so the suite can run next to your own dev stack. `scripts/stack test` adds 100 more for the scenarios |
| `PIPELINE_STATE_DIR` | `.devstack-pipeline/` | The stack's state dir. Reset at the start of each file; its `logs/` are kept afterwards. `scripts/stack test` sets it per file: `.devstack-pipeline/` for the full chain, `.devstack-scenarios/` for the scenarios |

Each file starts a stack of its own. `scripts/stack test pipeline` gives each its own ports and
state dir, so both keep their logs, and its summary names the `logs/` folder of a file that
failed; a plain `pytest tests/pipeline` run uses one state dir and keeps only the second file's
logs. To debug a failure, run that file alone (`scripts/stack test full-chain` or `scenarios`),
then read `<state dir>/logs/` or `scripts/stack logs SERVICE --state-dir <state dir>`. A file
that is stopped after its timeout gets SIGTERM first, so its stack still goes `down`. CI runs
`scripts/stack test pipeline` and uploads both `logs/` folders when it fails.

**`test_full_chain.py`** (8 tests) is one first-time user's session, in order: start from nothing
(`up` is ready and idempotent); the first signed-in request creates the user, and identity
headers from the browser are dropped; a prompt sent the chat UI's way reaches the seed agent over
A2A and comes back; the conversation is saved for that user only; the stream sends each step,
then one final event; an agent registered with `add-agent` receives the prompt that matches it; a
`user_id` in the request body is ignored when a login identity is present; `down` leaves no
process or port behind.

**`test_scenarios.py`** (12 tests) runs on a stack whose planner gives up on an agent after
5 seconds (`A2A_HTTP_TIMEOUT_SECONDS`; the default is 30), stops a turn after 6 tool steps
(`PLANNER_MAX_STEPS`; the default is 10) and asks before a write skill
(`PLANNER_POLICY_MODE=strict`), all passed through with `DEVSTACK_PASSTHROUGH_ENV`. Agents
added with `add-agent` have their saved definitions removed after each test. Agents the tests run themselves are published into the stack's directory with the
fake `dirctl` (`push`, then `routing publish`) and unpublished afterwards.

- **Credential scoping.** From one user's credential bundle, an agent whose card declares
  `openai_api_key` receives only that type, and an agent that declares nothing receives no
  credentials. A small A2A server inside the test records what each agent was sent.
- **Unreachable agent.** An agent that refuses the connection, or accepts it and never answers
  (once behind a card without streaming and once with it, which the planner calls with
  `message/stream`), ends the turn with a failed `call_agent` that names the agent, and the reply reaches the user:
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
  403, and another user's chat "yes" or "no" naming the conversation is refused; a confirm naming
  another prompt id gets `superseded` and runs nothing; the owner's confirm with the prompt's id
  runs it once with `confirmed` in its metadata; a second confirm fails; a chat "no" cancels it
  without running it. Two confirms sent together, with the agent slowed so the
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
- **Persistence and Postgres.** Registration runs with `FAKE_DB=1` and the chat app without a
  database: users and conversations live in memory, no Postgres constraints apply, and the
  chat app's credential store, grants and consent prompts are switched off. So the credential test
  sends the bundle as the request's `metadata.credentials`, the key under which the chat app's
  server hands stored credentials to the planner. It proves the scoping; storing, granting and
  consent need tier 2. The restart test proves the stack's own restart path: the registry is in
  memory, and `up` registers the seeds and the agents in `<state dir>/agents/` again. A
  confirmation or `ask_user` resumed on another chat app process needs tier 2.
- **Login.** The identity proxy adds `mail: dev@example.test`; Shibboleth and the ENGR gateway
  are not involved.
- **Deployments.** Agents run as local processes, not images. The ENGR cluster scripts and Cloud
  Run are not exercised; the Cloud Run backend has only mocked unit tests.

## Adding a test

- **Unit test:** put it in the component's `tests/` folder and run it with
  `scripts/stack test <component>`.
  Keep it offline and use the fakes already there: the planner's `ScriptedLLMClient`
  (`chat_app/planner/llm_client.py`), `DirectoryClient(fake_ads=True)` and the cards in
  `chat_app/tests/fixtures/agent_cards/`; registration's `FAKE_DB=1`
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
  for i in 1 2 3 4 5; do scripts/stack test pipeline || break; done
  ```

  The full-chain test passed 20 runs in a row on 2026-10-13; the suite with all eleven scenario
  tests passed 5 in a row on 2026-10-15 (about 32 seconds each).
- A `ConnectTimeout` on the first chat after a restart was traced to the planner leaving its LLM
  clients for the garbage collector to close at an arbitrary moment. It showed up when the
  restarted stack registered more agents than the restart test's own. It is fixed (one client per
  endpoint, closed on shutdown) and guarded by `chat_app/tests/test_llm_client.py`; a
  recurrence is a regression, not load.
- A security scenario that fails today goes in as a strict expected failure,
  `@pytest.mark.xfail(strict=True, reason="...")`: the suite stays green while the bug exists and
  fails as soon as the fix lands, so the marker is removed with the fix. None are open now.
