# Open-source release plan

Status as of **2026-10-12**. This is a working document for the maintainers and the
agents helping them. Update it as decisions land, and rewrite or remove it before the
repository goes public.

## Where things stand

- **Goal:** release AgenticNetwork as open source.
- **Approval:** the PIs and DI2 are waiting on the engineering clean-up before the
  release goes ahead. No license is attached yet. The approved license, the copyright
  line and any WashU sign-off still have to be recorded before visibility changes.
- **This week (from 2026-10-12):** a fix-up sprint. It works through the goals already
  set in the maintainers' release checklist, with the **full-pipeline test suite**
  (below) as the main deliverable to ship with the release.
- **Code state:** `main` = `ebf1b55` (2026-10-10). The handler-type consolidation
  (`llm` / `proxy` / `custom`) and the upload-impersonation guard have landed. Offline
  suites are green: registration 219, planner 76, ADS 22, in-repo skeleton 45.

## What ships

- **This repo, with `agent-skeleton` nested inside it at `agent_skeleton/` via
  `git subtree`** (decided 2026-10-12). The standalone
  [`washu-dev/agent-skeleton`](https://github.com/washu-dev/agent-skeleton) history is
  merged into `agent_skeleton/`. One plain clone gets everything, and changes can still
  be split back out with `git subtree push`. Only this repo needs to go public; the
  standalone repo can stay internal and its open hackathon PRs and issues never become
  public.
- **Proposed earlier, still to confirm:** `agent-directory` (the specialist agents and
  the hackathon-registered agents) stays out, as do model weights, cluster logs and
  WashU-specific operations details.

### Before the subtree merge

The two copies have diverged, so they must be reconciled first.

| Only in this repo's `agent_skeleton/` | Only in the standalone `agent-skeleton` |
|---|---|
| `container_runner.py` (the ENTRYPOINT of every built custom agent), `generator/`, the tool-loop `spec.py`, `system_tools/call_endpoint.py`, flat module layout, `test_generator` / `test_manager` / `test_operations` | `core/ custom/ tool_loop/ endpoint_wrapper/` layout, the `@tool` decorator, docs (`REGISTERING.md`, `KNOWN_ISSUES.md`), the 2026-07-27 hackathon fixes |

1. Port the repo-only pieces into the standalone layout. Keep the import path
   `agent_skeleton.container_runner`, because built images and
   `registration_service/archive.py` (the impersonation guard) depend on it.
2. Make both test suites pass in the standalone repo. Living at `agent_skeleton/`, it
   also avoids KNOWN_ISSUES #2 (its tests pass only from a folder of that name).
3. Replace the in-repo directory with a `git subtree add --prefix=agent_skeleton`.
   Without `--squash`, the standalone history (including the slide deck removed in
   `af3f25e`) becomes part of this repo's history. So either squash, or filter that
   history first. The release history is filtered anyway before anything goes public.
4. Update everything that reads the folder: `registration_service/builder.py` copies
   `agent_skeleton/` into each custom agent's build context, `backend.py` resolves it
   from the repo root, and the Dockerfiles install it.

## Sprint priorities

1. **The full-chain test, the one-command start and the laptop workflow** (Goals 1–3
   in the next section).
2. **Security fixes that must land before release**, each with a regression test in
   the suite. A 2026-10 review confirmed these issues:
   - a request-body `user_id` can override the logged-in identity;
   - one user can resume another user's pending confirmation;
   - an upload's `entry_file` can escape its folder (path traversal);
   - agents that declare no credentials receive the full credential bundle;
   - the skeleton copies credentials into the LLM prompt.
3. **CI:** a GitHub Actions workflow (none exists yet) that runs the four component
   suites and the pipeline suite on every PR.
4. **Hygiene:** the tracked `.gitignore` should cover `build/`, `dist/`, `Local/`,
   `.env*` (except examples), keys and model weights. No machine-specific files get
   committed.
5. **Merge the agent_skeleton copies** and bring the standalone history in with
   `git subtree` (see "Before the subtree merge").

## Full-pipeline test suite

### The three testing goals (2026-10-12)

**Goal 1: a full-chain test.** One test does everything a real first-time user's
session involves, in order, and checks each step:

1. **Start the system from nothing:** fresh database, empty directory, no services
   running.
2. **Create a user:** the user's first authenticated request creates their account.
3. **Send a whole prompt:** the user sends a complete request through the chat API,
   the same way the browser does.
4. **Get a response:** the planner discovers a specialist agent, delegates to it over
   A2A, and returns its answer to the user. The answer is also saved in that user's
   conversation history.
5. **Shut down cleanly:** nothing is left running and no state leaks into the next run.

By default it runs offline with a deterministic mock LLM, so it can run on every PR.
The same test can run against a real model when one is configured.

**Goal 2: start the system on command.** "Start the system" becomes one command that
is reliable enough for an agent, a script or CI to run whenever asked, and trust:

- It returns only when every service is healthy (exit code 0). Otherwise it fails
  quickly, with a message that names the broken service and points to its logs.
- It is idempotent: running it while the system is up changes nothing, and `down`
  then `up` works. After a restart, registered agents are discoverable again.
- It reports status in a machine-readable form (JSON), so an agent can check what is
  running without parsing logs.
- It has no hidden dependencies: no WashU cluster, SSO, personal files or keys. It
  defaults to the mock LLM.
- Reliability is measured, not assumed: it counts as done when the full-chain test
  passes many runs in a row on a clean machine.

**Goal 3: easy hands-on testing on a laptop.** A maintainer can try the whole system
by hand on a Mac in a few minutes. This replaces the untracked laptop prototyping stack
from summer 2026 with a tracked, documented one.

- **One command** brings up everything, and the chat UI opens at `localhost`, already
  signed in as a local dev user. There is no SSO and no cluster.
- **The LLM is switchable** with one setting: the mock LLM (offline, free, scripted
  answers), a hosted OpenAI-compatible API key, a local model (for example Ollama), or
  a tunnel to the cluster's vLLM.
- **Seed agents** are registered automatically, so there is something to talk to right
  away.
- **Trying an agent is one command:** run a folder from the `agent-directory` repo, or
  any local agent folder, through the real registration path, minus the GitHub
  commit. It then appears in the UI.
- **Everyday controls:** `status`, `logs <service>`, `reset` (wipe to a clean state)
  and `down`. Code changes show up without rebuilding images (source mounted into the
  containers).

The three goals share one start command. The full-chain test, an agent asked to
"start the system", and a person trying it by hand all use the same path, so the test
also checks the command people actually use.

### Scope of the suite

- **Request path:** user message → planner → directory search → Agent Card → credential
  gate → A2A call → specialist agent → final answer.
- **Registration path:** register → (build) → spawn → publish → routed → discoverable
  by the planner.

**Principles:** deterministic and offline by default (the planner's `ScriptedLLMClient`,
`FAKE_DB=1`, the fake directory); no real keys; the same scenarios rerun at higher tiers.

| Tier | Runs | What is real |
|---|---|---|
| 1. In-process | Every PR, in a few minutes | Planner, registration service and skeleton agents as local processes on free ports; scripted LLM; fake DB and directory |
| 2. Containers | Every PR or nightly | Tier 1 plus real Postgres (CHECK constraints) and real `dirctl` ("stored ≠ routed") |
| 3. Real LLM | Manual or nightly, opt-in | One round trip with a hosted or local model |

**First scenarios (proposed):**

- Register an `llm` agent → the planner finds it by skill → A2A call → answer.
- Each handler type: `llm` (no tools, presets, HTTP tools), `proxy`, `custom` upload.
  Legacy type names normalize on read.
- An agent built from the skeleton template, through each path (`endpoint_wrapper`,
  `tool_loop`, `custom`), works end to end.
- Credentials: a declaring agent receives only its scoped credentials; an undeclared
  agent receives none.
- `ask_user` and confirmation flows: only the owner can resume, including across
  processes.
- After the services restart, registered agents are rediscovered.
- Rejections: a body `user_id` is ignored when a login identity exists; traversal and
  impersonating uploads are refused.
- Failures: an unreachable agent yields a clear error, not a hang; the step limit holds.

Security scenarios that fail today are added as strict expected failures and flipped
when their fix lands. Proposed location: a top-level `tests/pipeline/` with helpers
that start each service.
