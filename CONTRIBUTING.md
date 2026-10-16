# Contributing

Thanks for helping. This file covers setup, tests, CI, pull requests and conventions. For what
the system is, start with [`README.md`](README.md); for how changes reach a running deployment,
[`docs/ONBOARDING.md`](docs/ONBOARDING.md).

## Development setup

Each component has its own venv, and their dependencies disagree (the planner pins `a2a-sdk`
0.3.26, the registration service and the directory tooling 0.3.2). Never install one component's
requirements into another's venv. Run everything from the repo root.

| Venv | For | Create it |
|---|---|---|
| `planner_agent/.venv` | the planner | `python3 -m venv planner_agent/.venv && planner_agent/.venv/bin/pip install -r planner_agent/requirements.txt` |
| `registration_service/.venv` | the registration service; also runs the skeleton, directory, dev stack and full-chain tests | `python3 -m venv registration_service/.venv && registration_service/.venv/bin/pip install -r registration_service/requirements.txt` (from the repo root: it installs `./agent_skeleton` in editable mode) |
| `agent_directory_service/.venv` | optional: only `agent_directory_service/run_ads.sh` against a real directory | `python3 -m venv agent_directory_service/.venv && agent_directory_service/.venv/bin/pip install -r agent_directory_service/requirements.txt` (uses the `buf.build` extra index named in that file) |

The dev stack needs Python 3.11 or newer as `python3`. CI uses Python 3.13 for the planner, 3.12
for the directory tooling and the full-chain test, and 3.11 for the rest. `scripts/stack doctor`
checks your setup.

## Running tests

Every suite runs offline: no Docker, Postgres, `dirctl`, external services or API keys. Do not set
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; the registration suite needs the `pytest-asyncio` plugin.

```bash
planner_agent/.venv/bin/python -m pytest planner_agent/tests -q
registration_service/.venv/bin/python -m pytest registration_service/tests -q   # its conftest sets FAKE_DB=1
registration_service/.venv/bin/python -m pytest agent_skeleton/tests -q
registration_service/.venv/bin/python -m pytest agent_directory_service/tests -q
registration_service/.venv/bin/python -m pytest devstack/tests -q

# Full chain: starts the dev stack on ports offset by PIPELINE_PORT_OFFSET (default 20000),
# drives it over HTTP, then stops it. Needs both service venvs.
registration_service/.venv/bin/python -m pytest tests/pipeline -q
```

The full-chain test uses its own state dir (`PIPELINE_STATE_DIR`, default `.devstack-pipeline/`),
so it can run while your own dev stack is up. To try a change by hand, use the dev stack:
`scripts/stack up`, `scripts/stack smoke`, `scripts/stack down` ([`docs/DEVSTACK.md`](docs/DEVSTACK.md)).
What each suite covers, and how to write tests: [`docs/TESTING.md`](docs/TESTING.md).

## What CI runs

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs on every pull request, on pushes
to `main`, and on demand:

- **unit**: the five suites above (planner, registration, skeleton, directory tooling, dev stack),
  each in a fresh environment, with `-p no:cacheprovider`.
- **repo hygiene**: no tracked file that `.gitignore` excludes; no tracked secrets, keys,
  credential or token files, model weights, slide decks, local databases, logs or wheels
  (`*.example` templates are allowed); no tracked file over 1 MB.
- **unit coverage**: each unit job also prints a line-coverage summary (report only, no threshold).
- **pipeline**: after the unit jobs pass, builds both service venvs, runs `scripts/stack doctor`,
  the full-chain test and the scenario tests (`tests/pipeline/test_scenarios.py`), and uploads the
  dev stack logs if either fails.

A separate workflow, `.github/workflows/compose.yml`, builds the Docker images and runs the
Compose smoke test (`deployment/compose/smoke.sh`) when a pull request or a push to `main` changes
deployment or service code.

Make sure the three `tests.yml` jobs (unit, repo hygiene, pipeline) pass on your pull request, and `compose.yml` when your change triggers it.

## Code conventions

There is no formatter or linter configuration in the repo: match the surrounding code (naming,
type hints, docstring and comment density). Also:

- Keep changes small and inside one component where you can.
- Add or update a test with every behaviour change, and a regression test with every security fix.
  Tests stay offline and deterministic: use the fakes (registration's `FAKE_DB=1`, the planner's
  fake directory and `ScriptedLLMClient`, the dev stack's fake `dirctl` and mock LLM), never real
  keys or services.
- New settings default to today's behaviour, so existing deployments keep working.
- Keep generated files, logs, local env files, bytecode and virtualenvs out of git.
- Do not hardcode specialist-agent endpoints into directory logic; read Agent Cards and records.
  Keep the directory (discovery) and A2A (task calls) separate.
- The A2A and directory code exists in several divergent copies (see
  [`CLAUDE.md`](CLAUDE.md) §9). When you fix a protocol or security bug, check the other copies.

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
