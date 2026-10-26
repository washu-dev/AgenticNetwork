# DTRC summer leftover ideas

Open work and ideas left over from the summer 2026 build of AgenticNetwork, during Washington
University in St. Louis's Digital Transformation Research Corps (DTRC). The list was drawn up after
the summer and cleaned up for this repository: statuses were checked against the code on
2026-10-20 (S4, S5, S27 and S57 updated 2026-10-21; S10, S22, S25 and S28 2026-10-25), and items that are finished or concern things outside this repository were removed.
Item IDs (S1–S109) are kept from the original working list, so gaps in the numbering are those
removed items. Nothing here is a commitment. Ideas are research directions, not features.

How to read the tables:
- **Type:** Task (a known change), Idea (a direction to explore) or Question (needs a decision).
- **Priority:** Important, Nice or None, for people using or extending the open-source release.
- **Effort:** S (hours), M (days), L (weeks or more).

---

## Start here

The items most worth picking up next, roughly in order. The first few are the project's most
distinctive design questions; the rest make it easier for newcomers to build on.

1. **S33: typed agent inputs.** The planner hands most agents a sentence, so an agent with many
   typed parameters can only use the few that fit in one text field. Letting cards declare an
   input schema per skill, and having the planner fill and validate it, is the clearest open design
   problem from the summer.
2. **S91: networks of specialized planners.** Today there is one planner and one directory. The
   direction is hubs, each with its own directory and planner, that pass work to each other. The
   first concrete step is wrapping a planner as an A2A agent so another planner can call it.
3. **S50 with S64: discovery at scale.** `list_agents` lists every agent, which stops working well
   as directories grow. Measure where selection accuracy drops, then move to retrieval.
4. **S34: agents that call other agents.** Only the planner can discover and call agents today;
   give the skeleton opt-in tools for it, with loop and depth limits.
5. **S72: an `examples/` folder,** one agent per onboarding path (S68, S69, S71).
6. **S82 with S103: a first-agent tutorial,** ending in a real request and response, plus a table of
   what an agent can send and receive today.
7. **S27: the remaining skeleton correctness issues,** including the tool loop's low default step limit.
8. **S107 with S28: open one GitHub issue per open item** and label the newcomer-friendly ones.
9. **S84: a one-to-two-page roadmap** built from the ideas in sections 4, 5, 7 and 10.

Plain-language terms used below:
- **A2A, Agent Card, directory (ADS):** see the root [`README.md`](../README.md#how-it-works).
- **DataPart.** A JSON attachment that travels inside an A2A message next to the text.
- **JSON Schema.** A standard way to describe the fields a JSON object must have and their types.
- **Package vs vendored copy.** A package is a versioned install (`pip install x==1.2`). A vendored
  copy is the whole library pasted into each project, so the copies drift apart over time.
- **AGPL.** A license that requires you to publish your source if you serve the software over a network.

---

## Done since the summer

For context, these summer items are finished and no longer listed:
- The registration handler types are consolidated into `llm`, `proxy` and `custom`, with the tool
  source (none, presets, HTTP operations, upload) as a field of `llm`.
- Upload checks: packages that claim to be the skeleton are rejected, and uploaded entry files must
  be plain file names.
- Credentials never reach the model-visible prompt, and by default an agent receives only the
  credential types it declares and the user granted.
- A one-command dev stack (`scripts/stack`), a Docker Compose tier, and CI that runs the unit,
  full-chain, scenario and Compose tests.
- All LLM calls use the Chat Completions API, so any OpenAI-compatible endpoint works (OpenAI,
  vLLM, Ollama).
- The in-repo `agent_skeleton/` carries every fix from the summer's separate skeleton copy.
- The chat app's approvals list shows every agent with uploaded code awaiting approval, not only
  `custom` ones; the skeleton-package upload error points to `agent_skeleton/README.md`; Slurm test
  instances (`PORT_OFFSET` ≠ 0) export their own `REGISTRATION_SERVICE_URL` (2026-10-21).
- The skeleton's demo agent keeps its tools in one file, `agent_skeleton/demo/tools.py`, that
  can be uploaded unmodified as an `llm` agent's tools module (S10, 2026-10-25).

---

## 1. Follow-ups from the summer refactor

| ID | Item | Type | Status (2026-10-20) | Priority | Next step | Effort |
|---|---|---|---|---|---|---|
| S6 | Test registration against a real Postgres for every agent shape, not only the in-memory test database | Task | Partly done. The Compose CI tier runs registration on real Postgres, but registers only the echo seed (an `llm` agent with no tools); the unit tests use the in-memory database | Important | Register one agent of each shape in the Compose tier: `proxy`; `llm` with presets, HTTP operations and an upload; `custom` with an upload and an approval | S |

## 2. Registration and agent ingestion

| ID | Item | Type | Status (2026-10-20) | Priority | Next step | Effort |
|---|---|---|---|---|---|---|
| S11 | Close the remaining gaps in uploaded-tools `llm` agents | Task | Partly done: streaming, credential stripping and `extra` reaching `build_tools` are fixed. Still open: the step limit is fixed at 4 with no per-agent setting; a module's own `normalize_result` is ignored; the served card has one generic skill instead of the registered skills and examples; the locally hosted path sends only the user text; there is no runtime cap like `HandlerExecutor`'s | Important | File each as its own issue. Add a per-agent `max_steps` (default about 8) first, together with S27 | M |
| S12 | Close the gaps in the `custom` (uploaded `AgentHandler`) path | Task | Open. `agent_skeleton/handler_executor.py` passes the handler only text and files (plus credentials and user id), not structured input or the task and context ids; the served card has one generic skill | Important | Pass structured input and the ids through; publish the registered skills; document reading per-request credentials from the context instead of writing them to process-wide environment variables | M |
| S14 | Harden the calculator preset tool against huge intermediate results | Task | Partly done. `agent_skeleton/system_tools/preset_tools.py` limits each exponent but not the size of intermediate results | Nice | Bound intermediate result size and add a test | S |
| S15 | Finish the wrapper plan: wire the multi-endpoint manager into registration (or drop it); decide the security posture for preset tools that touch the network or credentials | Task | Partly done. Presets and HTTP operations are in the registration UI; the multi-endpoint manager is an engine with tests only (`docs/DECISIONS.md` D13 is open) | Nice | Decide D13; wire or remove the manager; run one preset-tool agent against a real model | M |
| S17 | A corpus of synthetic agents (every handler type and upload shape) as regression tests for registration | Task | Partly done. `tests/pipeline/test_scenarios.py` covers registering and routing `proxy` and preset-tool `llm` agents; uploads (`custom`, uploaded-tools `llm`) aren't run end to end | Important | Add fixture agents and run register → upload → approve → route on each in CI | M |
| S18 | Automate bringing in outside agents: a scaffold, or a context file for a coding assistant, that turns an existing agent repo into a submission | Idea | Not started | Nice | Write the context file from `docs/BUILDING_AGENTS.md` and try it on a few public agent repos | M |
| S19 | Better dependency declaration | Idea | Partly done. Uploads support `pyproject.toml` / `setup.py`, `requirements`, `python_version` and `system_packages`, but the uploaded-tools form doesn't show the last two, `requirements.txt` isn't accepted, and imports aren't scanned | Nice | Accept `requirements.txt`, add the two form fields, add a warn-only scan for undeclared imports | M |
| S20 | Let people contribute just a tool (a function plus its schema), not a whole agent | Idea | Not started | Nice | Sketch it as an `llm` agent with `tools.source=upload` and a generated prompt | M |
| S21 | Decide where agents may write files, and how agents with large data files should ship them | Question | Not started. The upload archive cap is 25 MB (`registration_service/archive.py`) | Important | Document a rule (a per-request temp directory, nothing outside it) and the data-file path | S |
| S22 | Detect tool arguments that should be bound in a `build_tools(config)` closure instead of shown to the model | Idea | Not started. The alignment check (`validate_tool_registry` in `agent_skeleton/tools.py`) checks only that schemas match functions; `serve.py check` became `python -m agent_skeleton.demo check` on 2026-10-25 | Nice | Add an alignment-check hint for parameters sourced from config or the environment | M |
| S23 | Import outside agents that already publish A2A Agent Cards (for example, register by card URL) | Idea | Not started. The `proxy` type relays JSON to an upstream URL; it isn't an A2A client | Important (growth, and a stress test for S17) | Add registration by card URL | M |
| S105 | Decide who can contribute agents after the release | Question | Not started. `CONTRIBUTING.md` covers code and local registration, not outside submissions to a hosted instance | Important | Choose: self-hosted instances only, outside submissions through a hosted instance's approval queue, or both; record it in `docs/DECISIONS.md` | S |
| S108 | Decide whether endpoint-wrapper agents (`proxy`, and `llm` with HTTP tools) can use each user's own credentials, not only one operator token | Question | Open (`docs/DECISIONS.md` D6). `agent_skeleton/system_tools/call_endpoint.py` reads one operator environment variable | Important (otherwise a user can't wrap an API that needs their own account) | Decide it together with S15; if yes, reuse the declare → grant → inject path (D5) | S |

## 3. agent-skeleton and the contributor experience

| ID | Item | Type | Status (2026-10-20) | Priority | Next step | Effort |
|---|---|---|---|---|---|---|
| S24 | Publish `agent_skeleton` as a versioned package, so external agents pin a release instead of carrying a vendored copy | Task | Open. The in-repo copy is the single maintained skeleton; agents built during the summer each carry their own copy | Important | Publish a package and record the decision in `docs/DECISIONS.md` | M |
| S25 | Separate the template you copy from the library you install, so authors never edit installed files | Task | Partly done. The README was reworded on 2026-10-20 to call `tool_schemas.py`, `tools.py` and `prompt.py` the demo agent's files and say not to edit them. On 2026-10-25 the demo moved to `agent_skeleton/demo/`, which the runtime never imports and which no image, regular install or generator output includes (the registration image since 2026-10-26); anything under `demo/` is safe to edit, and everything else in `agent_skeleton/` is runtime. Still a flat layout with no template to copy | Important | Move to a `src/` layout and add a template in `examples/` or a `new-agent` command | M |
| S27 | Remaining skeleton correctness issues | Task | Partly done (2026-10-21): the tool loop now ends with an explicit step-limit message instead of an empty answer, and a handler result without a string `answer` fails the task cleanly. Open: (1) the default step limit is still `MAX_TOOL_STEPS=4` (`agent_skeleton/config.py`); (2) `HandlerExecutor.cancel` leaves the handler running; (4) `call_endpoint` with `protocol="a2a"` drops the structured payload | Important | Raise the default step limit (about 8); fix (2) and (4), each with a test | M |
| S28 | Newcomer conveniences: a local `serve-handler` / `test-handler` command to run and smoke-test an `AgentHandler` without registering; optional `.env` loading for it; a pre-serve structural check of an agent folder using registration's rules | Idea | Not started (`agent_skeleton/serve.py` has `serve-wrapper`; the demo's `check` and `serve` moved to `python -m agent_skeleton.demo` on 2026-10-25) | Nice (good first issues) | Label them for newcomers (S107) | S |
| S30 | Small docs fixes for contributors | Task | Open: document the reserved `agent_skeleton` package name in `docs/BUILDING_AGENTS.md` and `docs/REGISTRATION_SERVICE.md`; replace links to example agents in a separate, unpublished repository with an in-repo example (S72) | Important | One docs change, with S5 | S |

## 4. Planner, protocol and typed parameters

| ID | Item | Type | Status (2026-10-20) | Priority | Next step | Effort |
|---|---|---|---|---|---|---|
| S33 | A typed-parameter "manager" layer and structured A2A input. The planner's prose lands in one text field, so an agent with many typed parameters (for example, a paper-search agent with 17 filters) can use only the one that fits. Variants considered: a manager LLM that fills typed parameters with a tool call; a DataPart dictionary on the A2A message; "tools of tools" as a special tool call; moving away from natural language between agents | Idea | Partly there. `call_agent` accepts a free-form `task_input` that the planner sends as a DataPart and in metadata (`chat_app/planner/runtime.py` `_build_a2a_request`), and skeleton tool-loop agents merge it into the model's input. Missing: cards can't declare an input schema per skill, the planner doesn't validate against one, `custom` handlers and registration-hosted `llm` agents never see the DataPart, and `call_endpoint`'s A2A path drops it | Important (the clearest design problem) | Open a design issue with the paper-search numbers; prototype per-skill input schemas on one agent | M |
| S34 | Let agents call other agents: `list_agents` / `describe_agent` / `call_agent` as opt-in skeleton system tools | Idea | Not started. `agent_skeleton/system_tools/__init__.py` calls them "a planned addition"; only the planner has them. `agent_skeleton/README.md` already says agents can call other agents as tools | Important (core "internet of agents" claim) | Add them with depth and loop limits and identity and credential scoping; until then, reword the README claim (reworded on 2026-10-20: it now says only the planner calls agents) | M |
| S35 | Show what happened under the hood of a reply: which agents were called and where it failed | Idea | Not started in the chat view. The SSE trace event shows only "Thinking…"; traces appear in the admin view (`/admin/traces`) | Important (authors need to debug their agents) | Add a collapsible per-reply trace fed by the existing trace events and the final `tool_trace` | S |
| S37 | MCP: how this system relates to MCP servers; an MCP-backed example agent | Question | Not started; nothing in the repo mentions MCP | Important (one of the first questions outsiders ask) | Add a short "A2A vs MCP" FAQ paragraph; defer the example | S |
| S38 | Document exactly what a called agent receives from the planner | Task | Answered in code, not written down. `_build_a2a_request` sends the turn's user text, the planner's `task_input` (as a DataPart and in metadata), a skill hint, at most one uploaded file and the conversation id, but no history | Nice | Write it into `docs/BUILDING_AGENTS.md` | S |
| S40 | Let an agent hide irrelevant tools for a few loop steps, configured from metadata | Idea | Not started | Nice | Add to the roadmap (S84) | M |
| S41 | An optional prompt-rewriting step before the planner's tool loop | Idea | Not started. (The other half of the original item, showing authors the LLM response object, is in `agent_skeleton/README.md`) | Nice | Add to the roadmap (S84) | M |
| S103 | Widen what an agent can receive and return: files and images back to the user, jobs that outlive one HTTP stream, and multi-turn clarification between the user and a specialist | Idea | Partly there. Agents receive text, a DataPart and one uploaded file. The chat UI renders no file or image output; long jobs rely on streaming heartbeats and a 30-minute handler cap, with no push notifications or polling; a specialist's request for more input comes back to the planner, and the next call is a new task in the same conversation | Important (outside authors hit these limits first) | Add a table of what an agent can send and receive today to `docs/BUILDING_AGENTS.md`, then file each gap as an issue | M |

## 5. Directory, discovery and naming

| ID | Item | Type | Status (2026-10-20) | Priority | Next step | Effort |
|---|---|---|---|---|---|---|
| S42 | Fix name management: allow repeated names, make delete reliable, avoid commit races, show one status everywhere | Task | Open. `registration_service/server.py` returns 409 on a name-slug clash; agents are keyed by that slug; deleting an agent's committed build context is best-effort (`supervisor.py` only logs a warning); GitHub commits aren't serialized, so fast back-to-back approvals can get a 422 | Important | Key agents by an immutable id and show the name as a label. Serialize GitHub commits. Show the same status in every UI view | M |
| S43 | Keep the registry database and the committed agents in step | Task | Partly done. Startup no longer purges failed agents that built, and cluster recovery re-queues `llm` agents with uploaded tools; but `registration_service/db.py` `init_db` still deletes `stopped` rows, while their build contexts stay committed, and there is no reconcile view | Important | Replace the hard delete with a soft delete, and add an admin reconcile view that rebuilds rows from committed `agent.json` manifests | M |
| S45 | Card authoring: prefill the Agent Card from code or README metadata ("don't make the user retype"); suggest skills automatically; improve skill search and the nested skill menu; define how a user-written card and a generated card merge | Task | Open. The registration wizard has a skill picker with text search and a `has_routable_skill` check; nothing prefills or suggests | Important | Extract the card logic into one module, then add LLM-suggested skills and an optional bring-your-own-card mode | M |
| S46 | One path for every agent: remove the split between the built-in specialist agents and registered agents | Task | Open. `deployment/submit.sbatch` still starts a fixed set of built-in agents through a separate hand-written path | Nice | Register them through the registration service like any other agent, or drop them from the launcher | M |
| S47 | Version every agent, and allow choosing or pinning a version ("version control where specific orchestration can be chosen") | Idea | Partly done. Uploaded-code agents get an allowlisted `agent.json` manifest (`registration_service/build_manifest.py`); `llm` and `proxy` agent configs exist only in the database | Nice | Export `llm` and `proxy` configs (allowlisted, no secrets) the same way | M |
| S48 | Treat a new upstream commit as an agent update, gated by tests; ingest a GitHub repo as a folder holding the repo plus its dependency docs | Idea | Not started; registration has no update route | Nice | Add to the roadmap (S84) | M |
| S50 | Discovery at scale: `list_agents` must become retrieval, not a full listing, as directories grow toward thousands of agents | Idea | Open. `_tool_list_agents` (`chat_app/planner/runtime.py`) returns every agent; the directory backend (`agentnet/directory/backends.py`) caps search at 200 records with no warning and fetches each record with its own call | Important (scaling story) | Move to skill or semantic search with paging, warn on truncation, add a test with more agents than the limit, and measure where listing breaks (S64) | M |
| S51 | Agent heartbeats vs polling; a relational registry vs a lightweight denormalized directory | Question | Open design question. The supervisor polls agent health every 30 s | None | Record an answer in `docs/DECISIONS.md` only if it's still useful | S |

## 6. Deployment, scaling and cloud

| ID | Item | Type | Status (2026-10-20) | Priority | Next step | Effort |
|---|---|---|---|---|---|---|
| S52 | A tracked, one-command quickstart on a laptop | Task | Mostly done: `scripts/stack` and `docs/DEVSTACK.md` (mock, OpenAI-compatible, Ollama or vLLM model; seed agents; add an agent), plus Docker Compose (`docs/deployment/COMPOSE.md`), all run in CI. Still open: one recorded round trip with a real model, and `scripts/stack add-agent` for an agent folder or code upload (not only a JSON body) | Important (the main path for new users) | Do both | S |
| S54 | Finish the Cloud Run deployment: a public front door (HTTPS load balancer plus sign-in), and the rest of the services on Cloud Run | Task | Paused. `docs/deployment/gcp/DEPLOY_YOUR_OWN.md` doesn't cover a public front door and hasn't been re-run on a fresh project | Nice (the release doesn't depend on it) | Write the front-door guide and re-run the deploy guide end to end on a fresh project | L |
| S55 | On-demand agents everywhere ("scale to zero"), a demand-driven scheduler, and how many instances to run under load | Idea | Partly done. Cloud Run agents scale to zero (`registration_service/backend.py`); Slurm, Compose and the native stack start every agent at boot, and the chat app relies on a manual `CHAT_APP_HEALTH_PROBE_SKIP_AGENTS` list | Nice | Replace the manual list with a flag on the card or registry row; keep the scheduler on the roadmap | L |
| S56 | Hardware and GPU requirements in the Agent Card; GPU agents | Idea | Partly done. A resource-requirements card extension exists (`registration_service/card_resources.py`: cpu, memory, concurrency, max instances, gpu) for Cloud Run sizing; no backend hosts GPU agents yet | Nice | Document the extension as the standard, and document "GPU agents = a self-hosted endpoint registered as a proxy agent" until a GPU-capable backend exists | S |
| S58 | Development shouldn't depend on one person's accounts; rebuild one service without restarting everything | Task | Partly done. The dev stack and Compose need no personal accounts. Registration build commits still go out under a single token (`docs/MULTI_CONTRIBUTOR.md`; a GitHub App is planned) | Important | Move registration commits to a GitHub App or bot identity; document redeploying one service on Slurm | M |
| S59 | Agents on different cluster nodes | Question | Open; the Slurm deployment assumes one node (agents' card URLs are localhost), and the limit is documented | Nice | Support cross-node addresses, or keep the limit documented | M |
| S60 | Infrastructure as code instead of console and `gcloud` steps | Question | Open. Only an IAM script is scripted (`deployment/scripts/gcp_iam_step7.sh`); a plan is in `docs/deployment/gcp/MIGRATION.md` | Nice | Terraform/OpenTofu or Pulumi for the Cloud Run deployment | M |

## 7. Evaluation, verification and benchmarks

| ID | Item | Type | Status (2026-10-20) | Priority | Next step | Effort |
|---|---|---|---|---|---|---|
| S61 | A verifier agent: check that an agent does what its card claims ("do we take their description of the agent as truth?") | Idea | Not started. Admin approval checks code, not output quality (`docs/DECISIONS.md` D2) | Important (trust story; don't claim it as built) | Start with smoke tasks run at approval (S62), then an LLM verifier | L |
| S62 | Per-agent task tests and benchmarks; derive them from an agent's README or card; tell overlapping agents apart | Idea | Not started. `tests/pipeline` checks the platform, not each agent's quality | Important | Define a small task-file format (input plus expected check) and run it at approval and nightly | M |
| S63 | A routing benchmark: task files with known correct agents over a fixed agent set, to compare routing algorithms | Idea | Not started. The mock LLM, the fake directory and the pipeline harness could host it | Important (research hook) | Build it on `tests/pipeline` and `devstack/` | M |
| S64 | Measure agent-selection accuracy against directory size with a real model | Task | A harness exists outside this repo, with fake-model results only | Important (a credible scaling claim) | Bring the harness here, run the sweep on a real model, publish the results; pair with S50 | M |
| S65 | Load and stress tests, and committed data for any performance figures we cite | Task | Not started; no load harness or data is in the repo | Important | Drive N concurrent users through the API with the mock LLM, then a real model; publish concurrency and cold-start numbers per target. Cite no figures without committed data | M |
| S66 | Observability: track queries, results, agent use, latency and tokens; find where time goes (orchestration vs the LLM) | Idea | Not started. The planner returns a `tool_trace` per turn and logs token use, but nothing aggregates them | Nice | Aggregate the existing traces with a privacy-safe schema | M |
| S67 | Agent competition: send a task to the top two agents plus a random third, judge the answers, record the winner | Idea | Not started | Nice | Pair it with S62 once benchmarks exist | M |
| S104 | Multi-user behavior: several users at once; everyone sees the agents others add; nobody sees anyone else's data or credentials | Task | Mostly done. Pipeline and unit tests cover conversation ownership, only-declared credentials (none if undeclared) and owner-only confirmations (`tests/pipeline/test_full_chain.py`, `tests/pipeline/test_scenarios.py`). Not covered: stored credentials across users on Postgres, and truly concurrent traffic. `PLANNER_UNSCOPED_CREDENTIAL_AGENTS` remains an opt-in legacy allowlist | Important | Add a Postgres test that one user can't read or use another's stored credentials or grants; retire the legacy allowlist | M |

## 8. Example agents

| ID | Item | Type | Status (2026-10-20) | Priority | Next step | Effort |
|---|---|---|---|---|---|---|
| S68 | A paper-reviewer agent as a public example of wrapping existing research code | Task | Not in this repo; a working prototype exists separately. Before it ships: restrict what its PDF reader may open (only `http(s)` URLs, never local paths), keep its paper cache per request, and add a spending cap | Important (flagship "wrap existing code" example) | Apply those fixes, then add it as `examples/paper-reviewer` | M |
| S69 | An image-detection (YOLO-style) agent as a multimodal example, in both the uploaded-tools and custom forms | Task | Not in this repo; a prototype exists separately and was never registered on the platform. Its default backend should be the BSD-licensed torchvision one, not Ultralytics (AGPL) | Important (multimodal example) | Default to torchvision, register it on the dev stack (`scripts/stack`), record a short demo | S |
| S70 | Wrap a hosted biomedical research agent (Biomni) as an A2A agent, including long-running calls | Task | A client prototype exists outside this repo; it needs a hosted endpoint to stay available | Nice | Build the handler only if an endpoint is available; handle long jobs together with S103 | M |
| S71 | Ship one real endpoint-wrapper example against a public API (for example Open-Meteo) | Task | Not started. The dev stack seeds only an echo agent (`devstack/seeds/echo_assistant.json`); `tests/pipeline/test_scenarios.py` covers a proxy agent against a local stand-in, not a real API | Important (shows the no-code path) | Add a seed in `devstack/seeds/` that fronts the public API and check a live call | S |
| S72 | An `examples/` folder with one agent per onboarding path (wrap an endpoint, LLM tool loop, custom code) | Task | Not started; there is no `examples/` folder | Important | Assemble it from S71, S68 and S69 | M |
| S74 | More varied agents that aren't LLM wrappers: tabular models, image interpretation, video analysis, simulators | Idea | Not started in this repo | Nice | Pick one non-LLM agent for the examples gallery | L |
| S75 | Paper-review variants: wrap another hosted paper-review service; compare with "deep research" tools; read paywalled papers through institutional access | Idea | Not started | Nice | Add to the roadmap (S84) | M |
| S76 | A toolmaker agent, and a "what can this network do?" capabilities agent | Idea | Not started | Nice (a capabilities agent makes a good demo) | Add to the roadmap (S84) | M |

## 9. Docs and project tracking

| ID | Item | Type | Status (2026-10-20) | Priority | Next step | Effort |
|---|---|---|---|---|---|---|
| S81 | Keep the docs in step with the code; split AI-assistant instructions from human docs | Task | Partly done. `CLAUDE.md`'s internal address and old file-sharing section, and the dead handover pointer, are gone; one internal address remains in an operations doc (`docs/deployment/DOWNSTREAM_CONSIDERATIONS.md`). `CLAUDE.md` is still a single-file system guide serving both AI assistants and people | Important | Decide whether `CLAUDE.md` stays one file; check each doc against the code before release | M |
| S82 | A worked explanation of what an agent author must handle (request → handler → response) and a "build your first agent" tutorial that ends in a real request and response | Task | Partly done. `docs/BUILDING_AGENTS.md` and `agent_skeleton/` docs exist; there is no walkthrough with a real `curl` or A2A-client request | Important | Write one tutorial that ends in a real request and response | M |
| S83 | Architecture diagrams: what runs in containers, the hardware, how pieces talk, and one request flow per onboarding path | Task | Partly done. The root README has a system flowchart | Important | Add one request-flow diagram per onboarding path | M |
| S84 | A short roadmap: the deep open problems, what has been done, what needs doing | Task | Not started | Important (it seeds a public ROADMAP) | Write 1-2 pages from S33, S50, S61-S64 and S91-S92 | S |
| S107 | Track open work on GitHub: one labeled issue per open code item, on one project board | Task | Not started; the repo has no open issues | Important (shows where help is wanted; good first issues, see S28) | Open one issue per open code item in this list and label the newcomer-friendly ones | S |
| S109 | Make it useful to real users: have a few researchers add agents they would use themselves, and record what they ask for | Idea | Not started; no usage data or user feedback is recorded | Important (a launch with real use cases is far more credible) | Ask 3-5 people to use or add one agent for a real task; file their friction points as issues | M |

## 10. Bigger ideas and research directions

| ID | Item | Type | Status (2026-10-20) | Priority | Next step | Effort |
|---|---|---|---|---|---|---|
| S90 | Agent marketplace and incentives: revenue shared with authors when their agents' responses are used; compute-weighted pay; "an efficient marketplace between these agents and these tasks"; attribution | Idea | Not started | None for v1 (a strong vision line) | Write the vision paragraph for the roadmap (S84) | L |
| S91 | Hierarchical networks: hubs (each its own directory plus planner) with overlapping membership; a Task-Hub Alignment Agent (THAA) that decides what its hub can do and passes the rest to other hubs; horizontal inhibition among similar agents; "hot-lines" between agents that often work together; placing a new agent in the hub it adds least diversity to; many smaller planners instead of one | Idea | Prototype only, outside this repo (an offline simulation with 48 passing tests). In this repo, discovery is a plug-in (`DirectoryBackend` in `agentnet/directory/backends.py`), but the planner has no A2A endpoint or Agent Card, so planners can't yet call each other | Important (the main research direction; label it as research) | Phase 1: two directories and two planners on one host. Phase 2: wrap each planner as an A2A agent with a card, publish it into the other hub's directory, and carry a loop guard in request metadata | L |
| S92 | A routing sandbox mixing decentralized and centralized routing ("uploaded agents can use their own A2A routing algorithm"), and compute optimization ("tokenomics") | Idea | Not started | Nice | Add to the roadmap; it pairs with S63 | L |
| S93 | Tool and agent selection by minimizing the entropy of the model's choice; study LLMs' tool-choice behavior (for example bias by option position) | Idea | Not started; native function calling replaced it as a production mechanism | Nice (good launch content) | Measure choice entropy against option position with the S64 harness | M |
| S94 | Community tools vs system tools; cluster tool embeddings into "toolbox agents"; tool overlap across agents | Idea | Not started. `agent_skeleton/system_tools` ships `call_endpoint` and preset tools | Nice | Add to the roadmap after S20 | L |
| S95 | An institutional hub of cleaned research datasets, with agents to analyze them | Idea | Not started | None | Park it | L |
| S96 | A collaboration-finder agent: reviews datasets, methods and questions, and suggests high-impact collaborations between groups that don't usually work together | Idea | Not started | Nice (a good flagship demo agent) | Prototype it as an `llm` agent on the dev stack | M |
| S97 | A model informed by an institution's own knowledge, which can teach it and advise collaborations | Idea | Not started | None | Park it | L |
| S98 | Small-world organization; agents that post global skill or task requests; a shared example-data store; a shared space where agents note how to work together | Idea | Not started | None | Park it | L |
| S99 | A connector to coding agents (file-based handoff, parallel strands) | Idea | Not started | Nice | Add to the roadmap | M |
| S100 | Positioning: how this differs from single-agent coding assistants ("ecosystem") and from AGNTCY, whose directory it uses | Question | Not started. The docs name AGNTCY only as the directory implementation | Important | Draft one README paragraph | S |
| S101 | Look into related work such as SakanaAI's fugu | Question | Unknown | None | Skim it once; drop it if irrelevant | S |
| S106 | Learn cheaper routing from usage: log the planner's all-in-context choices and use them as labels for scalable retrieval-style routing. Compare top-down routing (split the prompt, choose through hubs) with bottom-up propagation, where agents in the same hub as active ones get boosted | Idea | Not started. The planner returns a tool trace per request but nothing aggregates traces (S66), and `list_agents` still lists every agent (S50) | Nice (research; links S66, S50 and S63) | Once S66 logs requests, agents chosen and outcomes, train a simple ranker and score it against plain listing with the S63 benchmark | L |
