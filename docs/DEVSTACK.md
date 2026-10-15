# Local dev stack

Run the whole system on your laptop with one command: the chat UI, the planner, the
registration service, a seed agent and a mock LLM. There is no Docker, no database
server, no SSO, no cluster and no API key. The default LLM is an offline mock.

This is Goal 3 in [`OPEN_SOURCE_RELEASE.md`](OPEN_SOURCE_RELEASE.md) ("easy hands-on
testing on a laptop"). Goals 1 and 2 (the full-chain test and "start the system on
command") use the same start command.

## Quick start

You need Python 3.11 or newer as `python3`, `lsof` (preinstalled on macOS; `down` and `reset`
use it to find leftover processes), and the two service venvs (one-time setup
below). From the repo root:

```bash
scripts/stack up                    # start everything; returns when it is all ready
open http://localhost:8080/app      # chat UI, already signed in as dev@example.test
scripts/stack down                  # stop everything
```

`scripts/stack` is a thin wrapper for `python3 -m devstack`; both work the same.

Try a prompt like "Echo this back: hello". The planner finds the seed agent *Echo
Assistant*, sends it your request over A2A, and shows its answer. With the mock LLM the
answer starts with `[mock-llm] Echo:`, which proves the agent's own LLM call ran.

### One-time setup: the venvs

The planner and the registration service pin different `a2a-sdk` versions, so each has
its own venv. Create them from the repo root (the registration install must run from
the root because it installs `./agent_skeleton`):

```bash
python3 -m venv planner_agent/.venv
planner_agent/.venv/bin/pip install -r planner_agent/requirements.txt
python3 -m venv registration_service/.venv
registration_service/.venv/bin/pip install -r registration_service/requirements.txt
scripts/stack doctor                # checks Python, both venvs, the fake dirctl and ports
```

## What runs

| Service | Port | What it is |
|---|---|---|
| `proxy` | 8080 | The front door. Stands in for SSO: it removes any identity headers the browser sends and adds `mail: dev@example.test`. Open the chat UI here. |
| `planner` | 8090 | `planner_agent`, with no database (users and conversations live in memory). |
| `registration` | 8091 | `registration_service`, with an in-memory database and the real supervisor, which spawns agents as local processes. |
| `mock-llm` | 8099 | A deterministic OpenAI-compatible LLM (`devstack/mock_llm.py`). Not started with `--llm openai`. |
| agents | 19200–19299 | Agents spawned by the registration service. |
| `directory` | none | A fake `dirctl` (`devstack/bin/dirctl`) that keeps the agent directory in one JSON file. Like the real directory, a record is only discoverable after `routing publish`. |

The services and the agents they spawn listen on `127.0.0.1` only. `--port-offset N`
adds `N` to every port, so you can run a second stack next to the first. Give it its own
`--state-dir` too, and keep the offsets of stacks that run at the same time at least 100
apart: the agent range is 100 ports wide.

After the services are ready, `up` registers the seed agents in `devstack/seeds/` through
the real `POST /agents` path. It waits until the planner's `GET /agents` lists them.

All runtime state lives in `.devstack/` (ignored by git): logs, pid files, the
directory file, uploads and agents you added. The registry is in memory, so `down`
forgets registered agents; the next `up` registers the seeds (and your added agents)
again.

## Everyday commands

| Command | What it does |
|---|---|
| `up [--llm mock\|openai] [--no-seed] [--timeout S] [--port-offset N] [--json]` | Start whatever is not already running, wait until everything is ready, then register the agents (and restart any that are not live). Safe to run again: if everything is up, it changes nothing. |
| `status [--json]` | What is running and ready, with URLs, pids, log paths and the agents the planner lists. An agent that is listed but does not answer counts as not ready. Exits 0 only when everything is ready. |
| `smoke [--prompt TEXT] [--json]` | Sends one prompt through the front door. Checks that a specialist answered and that the conversation was saved. |
| `logs SERVICE [-n N] [-f]` | Shows a log: `proxy`, `planner`, `registration`, `mock-llm`, `directory` (every dirctl call), `mock-llm-requests` (every LLM request and reply), or an agent's name or id. |
| `add-agent FILE.json` | Registers an agent from a registration body (see below). |
| `down [--json]` | Stops this stack's services and spawned agents, and reports anything still listening on its ports. A service of this stack whose pid file was lost is found by its log path and stopped too. It never stops another stack's services or agents: if a stack started from another state dir holds these ports, `down` names it (pid, port, state dir), prints the command that stops it and exits 1. `--json` prints the same report on stdout. |
| `reset` | `down`, then deletes what the stack created in `.devstack/` for a clean start. It removes the dir itself only if nothing else is in it. If `down` leaves anything running, `reset` exits 1 and deletes nothing, so the pid files and logs stay for the next try. |
| `doctor [--json]` | Checks the setup without starting anything. |

Every command accepts `--state-dir DIR` (default `.devstack/`, or `$DEVSTACK_STATE_DIR`).
`up` refuses a dir that is not empty and was not created by the stack. `down` in a dir
with no `stack.json` (a stack that never started) stops only what its pid files name.

**Exit codes:** 0 ok; 1 not ready (`status`) or something left running (`down`, `reset`);
2 usage error; 3 preflight failed (a missing venv, a busy port or a missing setting);
5 a service did not become ready; 6 smoke test failed. When `up` fails it names the
service and prints the last 40 lines of its log. `up --json` and `status --json` print
machine-readable JSON, so a script or an agent can check the stack without reading logs.

## Switching the LLM

The mock LLM is the default. It is scripted: the planner always lists agents, calls the
agent whose name and description best match your words, and returns that agent's reply.
Agents answer `[mock-llm] Echo: <your message>`.

To use a real model, set three values in the environment or in `.devstack.env` at the
repo root (ignored by git; `KEY=VALUE` lines), then run `scripts/stack down` and
`scripts/stack up --llm openai`:

```bash
OPENAI_BASE_URL=https://api.openai.com/v1   # any OpenAI-compatible endpoint
OPENAI_API_KEY=...
MAIN_AGENT_LLM_MODEL=gpt-4o-mini            # the planner's model
# DEVSTACK_SPECIALIST_MODEL=...              # optional: the agents' model (default: the planner's)
# DEVSTACK_LLM=openai                        # optional: make openai the default for `up`
```

The same three settings cover a local model and the cluster. For Ollama, use
`OPENAI_BASE_URL=http://127.0.0.1:11434/v1` with any non-empty key and a model you have
pulled. For the cluster's vLLM, open an SSH tunnel and point `OPENAI_BASE_URL` at it.
The stack does not save the key in `.devstack/`.

## Adding an agent

`add-agent` takes the same JSON body as `POST /agents` on the registration service.
[`devstack/seeds/echo_assistant.json`](../devstack/seeds/echo_assistant.json) is a
complete example of an `llm` agent:

```bash
cp devstack/seeds/echo_assistant.json /tmp/my_agent.json   # edit name, description, prompt
scripts/stack add-agent /tmp/my_agent.json
```

It returns once the planner lists the agent, so it is immediately available in the UI.
The definition is saved in `.devstack/agents/` and registered again on every `up`. To
drop it, delete that file (or run `reset`). With the mock LLM, the planner picks the agent
whose name and description share the most words with your prompt.

To add a seed for everyone, put a JSON file in `devstack/seeds/`.

## Code changes

Services run straight from your checkout. After editing code, restart with
`scripts/stack down && scripts/stack up` (a few seconds).

## Troubleshooting

- **`up` exits 3, "port ... is in use":** another program (or a stack from another
  `--state-dir`) holds the port. Stop it (`scripts/stack down --state-dir <that dir>` for
  a stack), or use `--port-offset 1000`.
- **`up` exits 5:** the message names the service and shows the end of its log. Read
  the full log with `scripts/stack logs <service>`. `up --keep-on-failure` leaves the
  started services running so you can poke at them.
- **The planner lists no agents:** check `scripts/stack logs registration` for the spawn,
  then the agent's own log (`scripts/stack logs "Echo Assistant"`), then
  `scripts/stack logs directory` to see whether `routing publish` ran.
- **An odd answer with the mock LLM:** `scripts/stack logs mock-llm-requests` shows every
  request and reply, for both the planner and the agents.
- **Something is still running after `down`:** `down` lists it with its pid and command
  (for another stack's service, the command that stops it). A service that did not stop
  keeps its pid file, so running `down` again retries it.
  `lsof -nP -iTCP:19200-19299 -sTCP:LISTEN` shows anything in the agent port range.
- **Anything else:** `scripts/stack reset`, then `scripts/stack up`.

## How it fits with the tests

- `devstack/tests/` covers the mock LLM, the fake `dirctl`, the identity proxy and the
  stack CLI's safety checks; CI runs it with the unit suites:
  `registration_service/.venv/bin/python -m pytest devstack/tests -q -p no:cacheprovider`.
- `scripts/stack smoke` is a one-prompt version of the full-chain test (Goal 1). The
  full-chain test drives the same commands (`up`, `status --json`, `down`) and treats
  the system as a black box, through its HTTP APIs and the stack CLI only. So it can also
  judge a component that was rebuilt from scratch.
- Service start commands live in one place, `_service_specs` in `devstack/stack.py`.

## Future work

- **Docker tier:** done separately as the Docker Compose stack, with real Postgres and a real
  `dir` daemon ([`deployment/COMPOSE.md`](deployment/COMPOSE.md)). Running these same `scripts/stack`
  commands against containers is not planned.
- **Agent folders:** `add-agent` takes a JSON registration body today. Running a folder
  from the `agent-directory` repo (a `custom` upload) through the same path comes next.
