# Agent Upload Lanes — Design Proposal

**Status:** Draft for discussion (not yet scheduled)
**Scope:** `registration_service` intake model + `agent_skeleton` build/run pipeline
**Audience:** maintainers deciding how the network should accept user-contributed agents
**Related:** [`CUSTOM_AGENT_HANDLER_PLAN.md`](CUSTOM_AGENT_HANDLER_PLAN.md), root [`../CLAUDE.md`](../CLAUDE.md) §6/§8/§9

---

## 1. The problem

Adding a real-world tool (e.g. a multi-module CLI like the `refbot` paper reviewer)
surfaces the limits of today's `custom` lane:

- **One file only.** The build context receives a single `handler.py`; a package
  with multiple modules can't be uploaded — it must be published to PyPI or pulled
  from a git URL. That pushes packaging work onto every uploader.
- **pip-only dependencies.** The generated Dockerfile ([`builder.py`](../registration_service/builder.py))
  runs `pip install` on `requirements` and nothing else. System binaries
  (`tesseract`, `poppler`, ffmpeg, …) can't be installed, so whole classes of
  agents (OCR, media, native libs) are impossible without a code change.
- **Trust rests on one AST scan.** [`validator.py`](../registration_service/validator.py)
  statically scans the single `handler.py` for a blocklist. This is already weak
  (bypassable via `__import__`, `getattr`, allowed imports) and becomes *meaningless*
  the moment we accept a repo, a Dockerfile, or a prebuilt image.

The deeper issue is that we haven't made an explicit decision about **how much the
platform builds and hosts vs. how much the uploader brings.** Each new need has been
met by extending the one `custom` lane ad hoc. This doc proposes a taxonomy so the
lanes are deliberate and share one pipeline.

## 2. What exists today

Three handler types (`registration_service/models.py`):

| `handler_type` | Uploader brings | Code? | Notes |
|---|---|---|---|
| `llm` | `system_prompt` + `model` + `tools[]` | no | declarative agent |
| `api_proxy` | `upstream_url` + `auth_header` | no | wraps an existing HTTP API |
| `custom` | one `handler.py` + pip `requirements` | yes | AST-validated, built into an image, `srun` on our node |

So the **no-code** story already exists; the gap is entirely in the **code** lane and
in the **"just point us at something already running"** lane, which is absent.

## 3. The three axes

Every intake option is a point in this space:

1. **What the uploader brings** — a spec / one file / an archive / a git repo / a
   container image / just a URL.
2. **Who hosts & runs it** — the platform (build + `srun` on the shared node) or the
   uploader (their own infra).
3. **How we establish trust** — and this *degrades* as axis 1 opens up: a spec or a
   single file can be statically vetted; a repo, a Dockerfile, or an opaque image
   cannot. Trust must then shift from *"vet the source"* to *"contain the runtime"*
   and *"authorize the endpoint."*

## 4. Proposed taxonomy: a unified `source_type`

Replace the implicit "custom = one file" assumption with an explicit `source_type`
on the registration record, so all lanes flow through one pipeline (register →
validate/build-or-verify → spawn/attach → publish to ADS) instead of growing
special cases:

| `source_type` | Uploader brings | Who hosts | Platform work | Trust model |
|---|---|---|---|---|
| `spec` | prompt/tools/model or api_proxy config (today's `llm` / `api_proxy`) | platform | render into skeleton | schema validation |
| `file` | one `handler.py` (today's `custom`) | platform | generate Dockerfile, build | AST scan + approval |
| `archive` | zip/tar of a code dir | platform | unpack, `COPY` tree, build | AST scan all `.py` + approval + **sandbox** |
| `git` | repo URL + ref (+ optional subdir) | platform | clone at ref, build | approval + **sandbox** (no static vetting) |
| `image` | prebuilt image ref (+ card) | platform | **no build** — verify + `srun` | approval + **sandbox** (opaque) |
| `remote` | live A2A endpoint URL + card | **uploader** | **no build, no host** — verify + register | **auth/allowlist**, not code review |

Each row reuses the same skeleton contract (`AgentHandler` / A2A card / ADS publish);
they differ only in how the runnable endpoint is produced.

### Explicitly out of scope: MCP as an intake lane

An earlier draft included an `mcp` lane (register an MCP server as an agent). It is
**deliberately excluded**, because A2A and MCP operate at different layers:

- **A2A** — agent ↔ agent: one autonomous agent delegates a whole task to a peer.
  This is the network/intake layer, and the directory already brokers it.
- **MCP** — agent ↔ tools: gives a *single* agent callable tools/resources from a
  server. An MCP server is a **toolbox, not an autonomous agent** — no planning, no
  task ownership.

Registering an MCP server as an agent in ADS is therefore a category mismatch (wrapping
a bag of functions and calling it a peer) and would add a *second inbound agent protocol*
to maintain alongside A2A for no gain — the network already has a sufficient protocol.

MCP's only non-redundant role is *inside* an agent: letting a specialist consume the
existing MCP tool-server ecosystem to expand its own capabilities. Even that is optional
today (agents already do tool access via plain function-calling), and it is a separate,
deferrable decision from agent **intake** — not part of this taxonomy.

## 5. Recommended priorities (early-stage)

Opinionated ordering by leverage-to-effort:

1. **`remote` (BYO endpoint) — make it first-class.** The directory is explicitly a
   phone book; decoupling *register* from *host* is the most scalable and
   philosophically aligned move (mirrors MCP remote servers). Serious agents want
   their own infra/secrets anyway. Lowest platform cost: verify the card, health-check
   the URL, publish to ADS. Turns "trust" into an **auth/allowlist** question
   (whose endpoints may we call; what creds do we forward) rather than code review.
2. **`image` (BYO prebuilt image) — highest-leverage hosted lane.** Dissolves *both*
   pain points at once: multi-file packaging **and** system deps (the uploader bakes
   `tesseract`/OCR into their image). refbot-with-OCR would "just work." We skip the
   build pipeline entirely and only `srun` + publish.
3. **`archive` (multi-file upload) — cheapest quick win** for uploaders who want us to
   build from source. Accept a zip, unpack into the build context, `COPY` the tree,
   AST-scan every `.py`.
4. Keep **`file`** as the absolute-beginner on-ramp, but stop treating it as *the* path.
5. **`git` / user-Dockerfile** last — most flexible, highest trust cost; only after
   sandboxing (§6) exists.

## 6. The load-bearing constraint: trust inverts as we open up

Today's safety = AST scan of one file + admin approval + container isolation. The
moment we accept `archive`/`git`/`image`, static analysis is worthless. **Decide the
runtime containment model up front** so every new lane plugs into the same sandbox
instead of each inventing its own checks:

- **Sandboxed execution** — rootless / gVisor / Firecracker, or at minimum
  locked-down enroot (no `--container-writable` on shared paths, dropped caps).
  Note the current posture: `coding_agent` already runs mini-swe-agent in YOLO mode,
  and custom agents `srun` on a shared node — this needs hardening before opening code
  lanes to untrusted users.
- **Network egress policy** — per-agent allowlist; default-deny outbound.
- **Resource quotas** — CPU/mem/time per agent; today they're unbounded `srun` steps.
- **No ambient secrets** — finish the per-user credential injection (auth §8) so
  agents receive scoped creds via task context, never the node's env.
- **Endpoint auth for `remote`** — signed cards / mutual auth / allowlist so the
  planner won't call arbitrary URLs and won't forward creds to unverified endpoints.

## 7. Open questions (decide before building)

1. **Who are the uploaders?** Internal trusted devs vs. the open public. This single
   fact decides whether approval + light isolation suffices, or real sandboxing is a
   prerequisite for *any* code lane.
2. **Do we want to be in the hosting business** for arbitrary agents, or is the
   platform primarily the directory + orchestrator with hosting pushed to uploaders
   (`remote`)? This sets how much to invest in build/host lanes vs. registration.
3. **Lifecycle for hosted agents** — cluster agents aren't auto-restarted today
   (known gap; see supervisor recovery). More hosted agents ⇒ need health/restart,
   quotas, teardown.
4. **Versioning & updates** — how does an uploader ship v2? Re-approve + rebuild?
   Immutable image tags? Matters most for `image`/`git`.

## 8. Suggested first step

Land the `source_type` field + the two lowest-cost, highest-leverage lanes
(`remote` and `image`) behind the existing approval gate, *plus* a minimal runtime
quota, before broadening the code-build lanes. That validates the taxonomy with the
smallest surface area and immediately unblocks agents (like refbot-with-OCR) that the
current pipeline can't express.
