# Custom Agent Lane v2 — Spec & Implementation Plan

**Status:** Finalized — all decisions locked (§2), integration-reviewed (§13), ready to break into PRs
**Scope:** `registration_service` intake/validate/build + `agent_skeleton` executor/runner
+ (credentials only) `planner_agent` discovery/call path + Settings UI
**Goal:** widen the existing `custom` lane so one lane spans **a single `.py` + pip
requirements** → **refbot-level agents** (multi-file projects, apt deps, slow/long-running,
per-user credentials), with no new `handler_type`.
**Non-goal:** the user-provided-Dockerfile / prebuilt-image lane (Appendix B: reasoning +
long-term plan for that as a separate, later lane).
**Related:** [`AGENT_UPLOAD_LANES_DESIGN.md`](AGENT_UPLOAD_LANES_DESIGN.md),
[`CUSTOM_AGENT_HANDLER_PLAN.md`](CUSTOM_AGENT_HANDLER_PLAN.md)

---

## 1. Principle

One definition of an agent: **a verified Agent Card + an A2A endpoint that obeys the
runtime contract** (Appendix A). The `custom` lane guarantees that *by construction* —
the user writes one adapter method; the skeleton supplies A2A/ADS/card plumbing. v2 keeps
that guarantee and only widens *what may ride alongside the adapter* (more files, more
deps), *how long it may run*, and *how it gets secrets*.

The adapter stays mandatory and user-authored — the platform does not auto-wrap arbitrary
code. `handler.py` bridges the user's code (e.g. `refbot`) into
`AgentHandler.handle_structured()`. v2's change is that the user's code now **rides along
in the upload** instead of having to be published to PyPI/git first.

## 2. Locked decisions (from design interview)

| # | Decision | Choice |
|---|---|---|
| D1 | Blocklist validation | **Advisory warning** surfaced to the approver; not a security boundary |
| D2 | Credential ownership | **User-supplied only** (BYO key); operator infra like vLLM base URL stays plain env |
| D3 | Credential declaration storage | **In the Agent Card / ADS record**, read by the planner at discovery |
| D4 | Grant flow | **First-use consent** (reuse planner `ask_user`) + revocable in **Settings** |
| D5 | Credential type namespace | **Predefined catalog + `custom` escape** |
| D6 | Enforcement | Each cred **required|optional**; **block** on missing *required*, proceed if only optional missing |
| D7 | Secret delivery | **Raw value** injected into A2A task context |
| D8 | Uploaded-project deps | **`pip install .`** when build metadata present, else PYTHONPATH; `requirements` additive |
| D9 | Upload UX | **`.zip`** upload |
| D10 | `system_packages` | **Arbitrary apt**, flagged for approver review |
| D11 | Runtime cap | **Configurable per-agent**, high default (~30 min), hard max |
| D12 | Credential declaration **carrier** | **`capabilities.extensions`** (a native A2A `AgentExtension`) — see §13 for why annotations don't work |
| D13 | Inspect endpoint (`GET /agents/{id}/handler`) for multi-file | **Return a downloadable zip** of the stored tree (+ manifest); single-file still returns text |
| D14 | Surface `required_credentials` to the planner LLM | **Yes** — add to `_agent_full_card` so the planner can explain needs during first-use consent |
| D15 | P1 exit criterion | **Hard gate on an end-to-end smoke test** (real register→approve→build→spawn→call + live `dirctl` round-trip), not just unit tests |

Verified in code during the integration review (§13) — these are facts, not assumptions:
- **Heartbeat is already supported planner-side** — `transport.py` uses SSE iff
  `card.capabilities.streaming`, consumes interim `TaskStatusUpdateEvent`s, and the httpx
  read timeout (`A2A_HTTP_TIMEOUT_SECONDS`, default **30s**) is between-bytes, so a beat
  more often than 30s keeps the call alive for the whole request. → P3 needs **no planner
  change**; heartbeat interval must be **< 30s** (use 20s).
- **Discovery is lossy through `annotations`** — the planner's `_normalize_for_internal`
  ([backends.py:486](../planner_agent/main_agent/directory/backends.py#L486)) keeps only the
  embedded A2A card (`modules[0].data.card_data`) and **discards the ADS `annotations` map**;
  there is **no live fetch** of the served card. So agent metadata the planner must see has
  to live **inside the A2A card**. `AgentCard` is `extra=ignore` (custom top-level fields
  dropped), but `capabilities.extensions` is a native field that round-trips → the carrier (D12).
- **Per-request credential injection into task context already exists** — `server._inject_credentials`
  → `runtime._build_a2a_request` (with Google-OAuth refresh). P4 reuses it; it does **not**
  need building.

## 3. Walls removed vs. retained

Removed by v2: single-file only; local package not importable; pip-only; one-shot/no
heartbeat; plaintext-`extra` secrets.
Retained (these define the boundary with the Dockerfile lane, Appendix B): non-Python
runtimes, GPU, persistent volumes, custom entrypoints, multi-stage builds.

---

## 4. Schema changes

### 4.1 `CustomConfig` ([registration_service/models.py](../registration_service/models.py))

```python
class CredentialRequirement(BaseModel):
    type: str                     # catalog key (see §8.1) or "custom:<slug>"
    label: str                    # human text shown in consent/Settings, e.g. "OpenAI API key"
    required: bool = True         # D6: required -> block if missing; optional -> proceed
    env_var: str | None = None    # optional: name the agent expects it under in context/env

class CustomConfig(BaseModel):
    class_name: str
    python_version: str = "3.11"
    requirements: list[str] = Field(default_factory=list)
    system_packages: list[str] = Field(default_factory=list)          # NEW (D10) — apt, cluster build only
    entry_file: str = "handler.py"                                     # NEW — adapter at archive root
    install_project: bool = True                                       # NEW (D8) — pip install . if metadata present
    credentials: list[CredentialRequirement] = Field(default_factory=list)  # NEW (D2/D3) — declared, user-supplied
    max_runtime_seconds: int = 1800                                    # NEW (D11) — clamped to HARD_MAX (e.g. 3600)
    extra: dict[str, Any] = Field(default_factory=dict)                # non-secret config only (secrets now via credentials)
```

- `system_packages` applies **only** to the cluster image build; `SubprocessBackend` logs
  a warning and skips them (can't apt-install per-agent locally).
- `entry_file` names the adapter module at the **upload root** that defines `class_name`.
- `install_project` gates `pip install .` (D8); ignored if no `pyproject.toml`/`setup.py`
  at root.
- `credentials` is the authoritative declaration; it is also written into the published
  card/record (§8.2). `extra` is now for **non-secret** knobs only.

No new `handler_type`. Internally record `is_archive: bool` on the agent row for
diagnostics; downstream lifecycle stays uniform.

---

## 5. Intake — single file OR .zip (P1)

### 5.1 Endpoint (`POST /agents/{agent_id}/handler`, [server.py:252](../registration_service/server.py#L252))

Dispatch on the uploaded filename/content:
- **`.py`** → today's path exactly: store as `handler_dir/handler.py` (regression-safe).
- **`.zip`** → validate + unpack into `handler_dir/` preserving structure; root is project root.

### 5.2 Storage layout (`HANDLER_FILES_DIR/{agent_id}/`, the `_handler_dir`)
```
handler.py            # entry_file — defines class_name (the AgentHandler subclass)
<user tree>           # e.g. refbot/, pyproject.toml — importable next to handler.py
```

### 5.3 Unpack safety (all must reject with 422 + clear message)
- **Zip-Slip:** every member must resolve inside `handler_dir` (no `..`, no absolute paths).
- **Symlinks:** reject any symlink member.
- **Size caps:** uncompressed total ≤ `MAX_ARCHIVE_UNCOMPRESSED` (default 25 MB) and
  member count ≤ `MAX_ARCHIVE_FILES` (default 2000); reject on overflow (guards zip bombs).
- **Reserved root names:** `agent_skeleton/`, `Dockerfile`, `*.card.json` collide with
  build-time injected files → reject.
- **Entry present:** `entry_file` must exist at root post-unpack.
- Clear existing `handler_dir` contents before unpacking a re-upload (re-upload resets
  `approved=False`, as today).

### 5.4 Inspect endpoint (`GET /agents/{agent_id}/handler`, [server.py:291](../registration_service/server.py#L291)) — D13
Today it `read_text()`s the single stored file. For a multi-file agent that would expose
only the adapter — a problem because **this is what the approver reviews before approving**
(the human trust boundary; §6). Change it to:
- **single-file agent** → return `{source, handler_file_path}` as today (regression-safe).
- **multi-file agent** → return a **downloadable zip** of `handler_dir/` plus a file manifest
  (list of relative paths), so the approver can inspect the entire uploaded tree.

---

## 6. Validation — refocused (P1)

`validate_handler` ([validator.py](../registration_service/validator.py)) operates on `entry_file` only:

- **Enforced (structural):** parses; defines `class_name`; `class_name` subclasses
  `AgentHandler`; defines `handle_structured`. Missing → 422.
- **Advisory (D1):** the import/call blocklist (`subprocess`/`socket`/`shutil`,
  `eval`/`exec`/`os.system`/`os.popen`) becomes a **warning list** returned alongside
  `ok=True`, stored on the agent row and shown to the approver in the approve UI. It does
  **not** block upload or approval.
- Scanning the whole tree is explicitly **rejected** as a design — it false-rejects
  legitimate deps (refbot's OCR path uses `subprocess`). State plainly in code + UI: the
  real boundary is **admin approval + container isolation** (long-term: sandbox,
  `AGENT_UPLOAD_LANES_DESIGN.md` §6).

Return shape becomes `ValidationResult(ok: bool, errors: list[str], warnings: list[str])`.

---

## 7. Build (P1 tree + P2 apt)

### 7.1 Build context committed by `supervisor._commit_to_github` → `agent-directory/registered_agents/{slug}/`
```
Dockerfile
{slug}.card.json
agent_skeleton/          # vendored (unchanged)
src/                     # the user's uploaded tree (handler.py at src root)
```

### 7.2 `builder.generate_dockerfile` ([builder.py](../registration_service/builder.py))
```dockerfile
FROM python:{python_version}-slim-bookworm
WORKDIR /app

# (only if system_packages) — D10
RUN apt-get update && apt-get install -y --no-install-recommends {system_packages} \
    && rm -rf /var/lib/apt/lists/*

# Framework (vendored)
COPY agent_skeleton/ /opt/agent_skeleton/
RUN pip install --no-cache-dir /opt/agent_skeleton

# User project tree
COPY src/ /app/

# (only if install_project and pyproject.toml|setup.py at /app) — D8
RUN pip install --no-cache-dir /app

# Additive/override pip requirements — D8
RUN pip install --no-cache-dir {requirements}

ENV PYTHONUNBUFFERED=1 HANDLER_FILE_PATH=/app/{entry_file}
ENTRYPOINT ["/usr/local/bin/python", "-m", "agent_skeleton.container_runner"]
```
Ordering rationale: apt first (so a C toolchain via `build-essential` is available to any
pip build), framework, then project, then `requirements` last so they win. `/app` is cwd
under `python -m`, so `import refbot` resolves and `HANDLER_FILE_PATH=/app/handler.py`
loads the adapter. **Note:** pip packages needing a compiler still require the user to add
`build-essential` (etc.) to `system_packages` — document, don't auto-add.

### 7.3 Local `SubprocessBackend` ([backend.py](../registration_service/backend.py))
- unpack to `handler_dir/`; `HANDLER_FILE_PATH=handler_dir/{entry_file}`.
- **add `handler_dir` to `PYTHONPATH`** (new — single-file handlers never imported
  siblings; multi-file needs the project root importable).
- `_ensure_venv`: `pip install .` (if metadata + `install_project`) then `requirements`.
- `system_packages`: log-and-skip with a warning.

### 7.4 Cross-repo dependency
The `agent-directory` GitHub Actions workflow must `docker build` the context directory
(`registered_agents/{slug}/`), not a hardcoded single file. **Confirm/patch it in the same
change** (separate repo).

---

## 8. Credentials (P4 — spans registration + planner + skeleton)

> Bigger than the lane work and touches three components. Could be split into its own
> companion doc/PR series; kept here for the full picture. Ships **after** P1–P3.

**Scope boundary (explicit).** P4 wires the per-user credential store for *newly-uploaded
custom agents* (declare → grant → inject), reusing the **same mechanism Google OAuth uses
today** (`user_credentials` + `crypto.py` + `/me/credentials/*`). **Migrating the existing
specialist agents off their environment-variable keys** (`BRAVE_API_KEY`, `GOOGLE_API_KEY`,
`GOOGLE_MAPS_API_KEY`) onto that same per-user path is a **separate, intended future
workstream** — tracked in [`AUTH_PLAN.md`](AUTH_PLAN.md) / CLAUDE.md §8 — and is **not**
part of P4. The maintainer explicitly wants to do it *at some point*; keep it a distinct
effort so P4 stays scoped to the custom-agent lane.

### 8.1 Credential catalog (D5)
A maintained catalog (new `registration_service/credential_catalog.py`, mirrored in the
planner) of known types, e.g.:
```
openai_api_key, anthropic_api_key, brave_api_key,
google_oauth (OAuth), google_maps_api_key, ...
```
Each entry: `type`, `label`, `kind` (`api_key` | `oauth`), optional `docs_url`. Plus a
`custom:<slug>` escape for anything uncatalogued (free-form label, `api_key` kind).
Declarations and user-stored keys both key off `type` so injection matches deterministically.

### 8.2 Declaration storage + planner read (D3, D12) — carrier is `capabilities.extensions`
Resolved by the integration review (§13): the planner's discovery **discards the ADS
`annotations` map** and keeps only the embedded A2A card, so the declaration must live
**inside the A2A `AgentCard`**. `AgentCard` is `extra=ignore`, but `capabilities.extensions`
(`list[AgentExtension]` — `uri`, `description`, `params: dict`, `required: bool`) is a native
field that round-trips. That is the carrier.

- **Write (registration):** in `supervisor._build_agent_card` ([supervisor.py:669](../registration_service/supervisor.py#L669))
  — *the card the planner actually reads* — add an extension built from the agent's declared
  `credentials`:
  ```python
  AgentExtension(
      uri="agenticnetwork.dev/ext/required-credentials",
      description="User credentials this agent consumes",
      required=any(c.required for c in creds),
      params={"credentials": [c.model_dump() for c in creds]},
  )
  ```
  appended to `AgentCapabilities.extensions`. (The served-card builders in
  `container_runner.py` / `custom_handler.py` may mirror it for direct callers, but the
  planner only reads the published one.)
- **Read (planner):** add `required_credentials: list[CredentialRequirement]` to
  `DiscoveredAgent` ([planner models.py](../planner_agent/main_agent/models.py)); populate it in
  `oasf_record_to_discovered_agent` by reading
  `interaction.agent_card.capabilities.extensions` for that `uri` (data is in `raw_record`).
- **Surface to LLM (D14):** include `required_credentials` in `_agent_full_card`
  ([runtime.py:1083](../planner_agent/main_agent/runtime.py#L1083)) so the planner can explain
  to the user why a key is needed during first-use consent; also expose it to the `call_agent`
  enforcement path (§8.4).

### 8.3 User key storage + Settings (D2, D4)
- Reuse `user_credentials` (+ `crypto.py` AES-256-GCM) and `/me/credentials/*`
  ([credentials_router.py](../planner_agent/main_agent/credentials_router.py)); extend rows to key by catalog `type`.
- New **Settings tab** in the planner web UI: list stored keys/authorizations (add/update/
  delete), and list **per-agent grants** (view/revoke). No secret values ever shown back.

### 8.4 Grant + consent flow (D4, D6)
Hook point: in `_tool_call_agent` **before `_build_a2a_request`**
([runtime.py:607](../planner_agent/main_agent/runtime.py#L607)), after the agent is resolved and
the policy decision is made. Reuse the existing `PolicyDecisionStatus.requires_confirmation`
gating pattern for the consent interrupt rather than inventing a new one.
1. Read `agent.required_credentials`.
2. For each **required** cred: is it (a) stored by the user and (b) granted to this agent?
   - missing/ungranted stored key → **block**: return a message via the `ask_user`
     primitive prompting the user to add the key and/or grant access (this is the
     first-use consent). Do **not** dispatch.
   - stored but not yet granted → consent prompt to grant (remembered afterward, revocable
     in Settings).
3. Optional creds absent → proceed.
4. Persist grants in a new `agent_credential_grants` table `(user_id, agent_id, cred_type,
   granted_at)`.

### 8.5 Injection + agent read (D7)
- **Injection already exists** (§13): `server._inject_credentials` fetches+decrypts the
  user's creds into request metadata, and `runtime._build_a2a_request`
  ([runtime.py:894](../planner_agent/main_agent/runtime.py#L894)) puts `metadata["credentials"]`
  into the outbound A2A task. P4 only needs to **scope** that to *granted* creds for the
  target agent (filter by the grants table) and mirror to declared `env_var` names — not
  build the path.
- `agent_skeleton` seam: extend `HandlerExecutor.execute` to extract credentials from the
  request context and pass them via a backward-compatible optional param:
  ```python
  async def handle_structured(self, user_input, files=[], context: dict | None = None) -> dict
  ```
  The executor inspects the handler signature; existing 2-arg handlers keep working.
  `context` carries `{"credentials": {...}, "user_id": ...}`. **Never log or persist
  `context`.**

### 8.6 refbot note
On the cluster refbot needs only `OPENAI_*` pointing at vLLM — **operator env, not a
credential** (D2). So refbot can ship with **zero** declared credentials today; the
credential machinery matters for agents calling user-owned external APIs.

---

## 9. Heartbeat / long-running (P3)

Verified: **no planner change needed.** Agent-side changes:
- Set `AgentCapabilities(streaming=True)` in **all three** runtime-authoritative card
  builders (§13) — they are near-duplicates and must stay in sync:
  **`supervisor.py:669` `_build_agent_card`** (the card published to ADS → the one the
  planner reads to decide whether to stream — *miss this and the heartbeat silently
  no-ops*), `container_runner.py:75` (cluster serve), `custom_handler.py:41` (local serve).
  (`builder.py:64` is informational-only.) This is the same set that carries the credential
  extension (§8.2).
- `HandlerExecutor.execute` ([handler_executor.py](../agent_skeleton/handler_executor.py)): run
  `handle_structured` as a task; while awaiting, emit a `working` `TaskStatusUpdate` every
  `HEARTBEAT_SECONDS=20` (**< 30s** read timeout). Enforce `max_runtime_seconds` (D11):
  cancel + emit a failed status if exceeded. Backward-compatible; fast handlers emit no beat.
- Bonus: interim updates already reach the planner `progress_callback` → UI trace, so a
  beat can carry real progress text.

---

## 10. Phasing

| Phase | Deliverable | Components | Unblocks | Risk |
|---|---|---|---|---|
| **P1** | `.zip` intake + unpack safety + multi-file storage + `pip install .`/PYTHONPATH + `COPY src/` + validator refocus (warnings) | registration, skeleton runner | locally-built multi-module refbot | Med (unpack safety, cross-repo Actions build) |
| **P2** | `system_packages` → apt (flagged for approval) | registration | OCR / native deps | Low |
| **P3** | `streaming=True` + heartbeat + `max_runtime_seconds` | skeleton | slow agents survive timeout | Low |
| **P4** | Credential catalog + card declaration + planner read/enforce/inject + Settings UI + `context` seam | registration + **planner** + skeleton | user-owned external API keys | Med–High (3 components, gated on auth §8) |

P1+P2 fully ingest a locally-built refbot (text + OCR). P3 hardens it. P4 is the largest
and can be its own PR series.

**P1 exit criterion (D15) — hard gate.** P1 is not "done" on unit tests alone; it must pass
an end-to-end smoke test on the real cluster: register a trivial **multi-file** agent →
approve → GitHub Actions builds the image → host loop spawns it → the planner discovers and
successfully calls it. Includes confirming `capabilities.extensions` survives a live
`dirctl push → routing search → pull` round-trip (real binary, not the fake). This catches
integration gaps unit tests with fakes can't (dirctl field survival, the Actions build,
port/spawn). Requires cluster access to sign off.

---

## 11. Testing (extend `registration_service/tests`, FAKE_DB + fake_supervisor)
- single-`.py` upload unchanged (regression).
- `.zip`: tree stored; `entry_file` found; sibling `import` resolves in the subprocess runner.
- unpack rejections: Zip-Slip, absolute path, symlink, oversize, too-many-files, reserved
  name, missing `entry_file` → 422.
- `builder.generate_dockerfile`: apt line iff `system_packages`; `pip install /app` iff
  `install_project` + metadata; `COPY src/` layout — snapshot.
- validator: structural failure → 422; blocklist hit → `ok=True` + `warnings` populated.
- (P3) executor emits ≥1 heartbeat for a slow scripted handler; none for a fast one;
  `max_runtime_seconds` exceeded → failed status.
- (P1, D13) `GET /agents/{id}/handler`: single-file returns text; multi-file returns a zip
  + manifest.
- (P4) the credential `AgentExtension` round-trips: publish → planner discovery →
  `DiscoveredAgent.required_credentials` populated (unit-level with a stubbed record; the
  live dirctl round-trip is covered by the P1 smoke gate, D15); required-missing → blocked
  with consent prompt; optional-missing → proceeds; injected creds are scoped to *granted*
  ones; `context` never logged.
- **P1 smoke gate (D15):** the end-to-end cluster run above is a required, tracked check —
  not a unit test — and blocks P1 sign-off.

## 12. Backward compatibility
- `entry_file` defaults `handler.py`; `.py` upload stored identically; empty
  `system_packages`/`credentials` render today's effective image + behavior.
- Existing tests pass except Dockerfile-string snapshots (update).
- Existing single-file agents need no migration.

---

## 13. Integration review — findings

Read-only review of the surfaces this plan touches but doesn't rewrite. All decisions were
held for the maintainer; nothing below changed code.

**Confirmed safe (no breakage to untouched functionality):**
- New optional `CustomConfig` fields — no `extra="forbid"`, config is schemaless JSONB, all
  consumers read via `.get()`, `AGENT_CONFIG_JSON` passes them through, no DB migration.
- `validate_handler` gaining `warnings` — sole caller reads only `.ok`/`.errors`.
- Heartbeat/`context`/`max_runtime` blast radius is **contained to custom agents** — `llm`
  and `api_proxy` use their own executors (`LLMAgentExecutor`, `APIProxyExecutor`); only
  `HandlerExecutor` changes. Optional `context` 3rd arg is safe (validator doesn't enforce
  arity; `inspect.signature` precedent at `runtime.py:439`).
- `DiscoveredAgent.required_credentials` extension is safe — optional/defaulted;
  `validate_oasf_record` isn't strict on extra keys; `_agent_full_card` hand-picks fields.
- Enforcement is a **strict no-op** for existing agents (empty `required_credentials` is
  falsy → check skipped).
- `src/` layout doesn't disturb image naming or the GH Actions `registered_agents/**` trigger.
- Per-request credential injection into task context **already exists** → reduces P4 scope.

**Flags raised and their resolution:**
1. *(critical, resolved)* Publish/read schema seam: registration publishes OASF 1.0.0
   (`annotations` + `modules[0].data.card_data`); the planner's `_normalize_for_internal`
   keeps only the embedded card and **discards `annotations`**, with **no live card fetch**.
   → credential declaration must ride in the **A2A card via `capabilities.extensions`** (D12),
   not annotations.
2. *(medium, folded into §9)* `streaming=True` must be set in **three** synced card builders
   incl. `supervisor.py:669` (the ADS-published card the planner reads) — else the heartbeat
   silently no-ops.
3. *(low, decided — D13)* `GET /agents/{id}/handler` degrades for multi-file → return a
   downloadable zip so the approver can review the whole tree.
4. *(low, downgraded)* dirctl tolerance of unknown fields is now moot for correctness (we use
   `capabilities.extensions`, a card field proven to round-trip in code); still exercised by
   the P1 smoke gate (D15).
5. *(info)* 3–4 near-duplicate `AgentCard` definitions are a standing maintenance hazard the
   plan leans on (§9); a future cleanup could unify them.

---

## Appendix A — Agent Runtime Contract (shared invariant)

Both v2 `custom` and the future Dockerfile lane must satisfy this; the skeleton satisfies
it automatically:
- **Serve A2A** on `AGENT_CONFIG_JSON.port` (fallback `AGENT_PORT`), bound `0.0.0.0`.
- **Read config** from `AGENT_CONFIG_JSON` (envelope: `agent_id`, `port`,
  `config{class_name,name,description,extra,credentials,…}`).
- **Card** at the well-known A2A path; **authoritative ADS record is platform-generated
  from the registration form** so skills/OASF routing keys stay canonical regardless of lane.
- **Credentials** arrive per-request in task context (never baked into the image).
- **Readiness:** port accepts connections + card fetch succeeds.

---

## Appendix B — Long-term: user-provided-Dockerfile lane (reasoning & goals)

**Why a separate lane, not more knobs.** v2 adds `system_packages`, `entry_file`,
`install_project`. Each knob after that (env files, build args, extra ports, multi-stage,
non-Python base) steps toward reinventing a Dockerfile through config. Inflection point,
stated explicitly: **when the config knobs start to approximate a Dockerfile, hand over
the Dockerfile.**

**What it is.** Intake where the user uploads a build context **including their own
`Dockerfile`** (or names a base image). Platform still owns the invariants (Appendix A),
card/ADS record generation from the form, spawn/health/teardown, provenance. User owns
only how the image is built. Covers everything v2 excludes (non-Python, GPU, native setup,
custom entrypoints, baked weights).

**The guarantee flips (crux).** v2 = *guarantee-by-construction* (fill one method, cannot
produce a non-conformant agent). Dockerfile lane = *guarantee-by-verification*: build
anything, then **test conformance at intake** — boot in a sandbox, fetch the card, send a
probe `message/send`, confirm a well-formed result. Pass → register; fail → reject.
Consistency is preserved at the layer that matters (verified card + A2A), not construction.

**Trust posture.** Opaque build ⇒ AST meaningless ⇒ lean on approval, **runtime isolation**
(the `AGENT_UPLOAD_LANES_DESIGN.md` §6 sandbox becomes a prerequisite), **provenance**
(pin by digest, registry allowlist), default-deny egress. Reasonable to restrict this lane
to vetted users while `custom` stays open.

**Bridge.** Publish a base image (`agent_skeleton` + `a2a-sdk` + `container_runner`
preinstalled, runtime contract documented) so a user Dockerfile can
`FROM agenticnetwork/agent-base` and inherit conformance — barely more work than v2, build
fully in the user's hands.

**Convergence & goals.** Both lanes end at one representation: built image → `srun` on an
assigned port → verify → publish platform-authored card → tracked identically. A
`source_type` (`file|archive` vs `dockerfile|image`) distinguishes **intake only**;
downstream is one pipeline — the end state the `source_type` taxonomy in
`AGENT_UPLOAD_LANES_DESIGN.md` §4 aims at. Long-term: (1) keep v2 `custom` as the paved
road; (2) add the Dockerfile/image lane behind verification + sandbox; (3) unify downstream
under `source_type`; (4) make sandbox + provenance + egress the standing trust model so new
intake lanes are cheap and safe.
