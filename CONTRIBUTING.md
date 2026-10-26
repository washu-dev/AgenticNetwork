# Contributing

Thanks for helping. This file covers setup, tests, CI, pull requests and conventions. For what
the system is, start with [`README.md`](README.md); for how changes reach a running deployment,
[`docs/ONBOARDING.md`](docs/ONBOARDING.md).

## Development setup

Both services, the agent runtime (`agent_skeleton/`), the shared `agentnet/` package, the dev
stack and every test suite run from one dev venv, `.venv` at the repo root, on one `a2a-sdk`
version (0.3.2). Run everything from the repo root.

| Venv | For | Create it |
|---|---|---|
| `.venv` | everything: both services, the dev stack, every test suite, `scripts/ads` | `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt` (from the repo root: it installs both services' requirements, `./agent_skeleton` in editable mode, and `pytest-cov`) |
| `agentnet/.venv` | optional: only `scripts/ads` with `ADS_BACKEND=sdk` (the default `cli` backend runs in the dev venv) | `python3 -m venv agentnet/.venv && agentnet/.venv/bin/pip install -r agentnet/requirements-sdk.txt` (uses the `buf.build` extra index named in that file), then set `ADS_PYTHON=agentnet/.venv/bin/python` |

Each service image still installs only its own requirements file (`chat_app/requirements.txt`,
`registration_service/requirements.txt`). The older per-service venvs (`chat_app/.venv`,
`registration_service/.venv`) are no longer used and can be deleted.

The dev stack needs Python 3.11 or newer as `python3`. CI runs every unit suite on Python 3.11
and 3.13 (the images' two versions) and the full-chain test on 3.12. `scripts/stack doctor`
checks your setup.

## Running tests

Every suite runs offline: no Docker, Postgres, `dirctl`, external services or API keys. One command
runs them, each suite in its own pytest process with the dev venv's interpreter:

```bash
scripts/stack test                     # everything: the unit suites, then the pipeline
scripts/stack test unit --parallel     # the unit suites at once
scripts/stack test registration_service agentnet   # some suites
scripts/stack test devstack -- -k sweep -x         # after --: pytest's own arguments
scripts/stack test --list              # what would run, with which interpreter
```

It exits 0 when every suite passed, 7 when one failed, and 3, naming the fix, when the venv or a
test plugin is missing (the registration suite needs `pytest-asyncio`; `--cov` needs `pytest-cov`).
Each suite gets a clean environment, so a stray `FAKE_DB=0` or `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`
in your shell cannot change the result. Underneath, each suite is one pytest call from the repo
root, such as `.venv/bin/python -m pytest registration_service/tests -q -p no:cacheprovider`
(its conftest sets `FAKE_DB=1`); `--list` shows them all.

The pipeline (`scripts/stack test pipeline`, needs the dev venv and `lsof`) starts the dev stack once
per file, one after the other, on ports offset by `PIPELINE_PORT_OFFSET` (default 20000; the
scenarios add 100) and in state dirs of its own (`.devstack-pipeline/`, `.devstack-scenarios/`),
so it can run while your own dev stack is up. To try a change by hand, use the dev stack:
`scripts/stack up`, `scripts/stack smoke`, `scripts/stack down` ([`docs/DEVSTACK.md`](docs/DEVSTACK.md)).
What each suite covers, and how to write tests: [`docs/TESTING.md`](docs/TESTING.md).

## What CI runs

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs on every pull request, on pushes
to `main`, and on demand:

- **unit**: the unit suites above (chat app, registration, skeleton, agentnet, dev stack), one
  job per Python version (3.11 and 3.13), each in a fresh environment from
  `requirements-dev.txt`: each job runs `scripts/stack test unit --python <its interpreter> --cov`.
- **repo hygiene**: no tracked file that `.gitignore` excludes; no tracked secrets, keys,
  credential or token files, model weights, slide decks, local databases, logs or wheels
  (`*.example` templates are allowed); no tracked file over 1 MB.
- **unit coverage**: each unit job also prints a line-coverage summary (report only, no threshold).
- **pipeline**: after the unit jobs pass, builds the dev venv, runs `scripts/stack doctor`,
  then `scripts/stack test pipeline` (the full-chain test and the scenario tests,
  `tests/pipeline/test_scenarios.py`), and uploads the dev stack logs if either fails.

A separate workflow, `.github/workflows/compose.yml`, builds the Docker images and runs the
Compose smoke test (`deployment/compose/smoke.sh`) when a pull request or a push to `main` changes
deployment or service code.

Make sure the three `tests.yml` jobs (unit, repo hygiene, pipeline) pass on your pull request, and `compose.yml` when your change triggers it.

## Code conventions

There is no formatter or linter configuration in the repo: match the surrounding code (naming,
type hints, docstring and comment density). Also:

- Keep changes small and inside one component where you can.
- Add or update a test with every behaviour change, and a regression test with every security fix.
  Tests stay offline and deterministic: use the fakes (registration's `FAKE_DB=1`, agentnet's
  fake directory, the planner's `ScriptedLLMClient`, the dev stack's fake `dirctl` and mock LLM),
  never real keys or services.
- New settings default to today's behaviour, so existing deployments keep working.
- Keep generated files, logs, local env files, bytecode and virtualenvs out of git.
- Do not hardcode specialist-agent endpoints into directory logic; read Agent Cards and records.
  Keep the directory (discovery) and A2A (task calls) separate.
- The A2A and directory code in this repo is one package, `agentnet/`; fix it there. The
  `agent-directory` repo has its own divergent copies (see [`SECURITY.md`](SECURITY.md) §3). When
  you fix a protocol or security bug, check those copies too.

## Branches and pull requests

- `main` is the default branch. Work on a feature branch and open a pull request against `main`;
  don't push to `main` directly.
- Keep each pull request to one change. Say what changed, why, and how you tested it (which
  suites, and whether you ran the dev stack).
- Update the docs in the same pull request when behaviour, commands or settings change.

## Adding an agent

Register one through the registration service (`llm` and `proxy` agents need no code; `custom`
uploads code), build on `agent_skeleton/`, or write a standalone A2A agent:
[`docs/BUILDING_AGENTS.md`](docs/BUILDING_AGENTS.md). In the dev stack,
`scripts/stack add-agent FILE.json` registers one locally; to add a seed agent for everyone, put a
registration body in `devstack/seeds/`.

## Docs conventions

- Docs live in [`docs/`](docs/README.md); add every new doc to the map in `docs/README.md`.
  Component READMEs hold that component's install and run steps. Deployment guides go in
  `docs/deployment/`; the machinery they describe stays in `deployment/`.
- Record durable design decisions in [`docs/DECISIONS.md`](docs/DECISIONS.md), newest first; when
  one is reversed, mark it superseded and add a new entry instead of rewriting it.
- Write plain, concise English. Every command, path, setting and env var you name must exist in
  the repo; check it.
- Use `example.com` or `example.test` addresses in examples, never real personal data.
- `docs/dev/` is ignored by git; use it for local working notes.
- If you change a test command or CI, update `CLAUDE.md` §10 to match.

## Reporting security issues

Do not open a public issue for a vulnerability. Report it privately as described in
[`SECURITY.md`](SECURITY.md).
