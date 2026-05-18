# Coding Agent

Standalone, self-contained A2A service that fronts `mini-swe-agent` with an
**LLM planner**. The user sends a natural-language prompt; the planner decides
whether to write code, whether to email it anywhere, and what files to ship.

This directory has no runtime dependency on any sibling package. The shared
A2A SDK boundary, directory client, and Pydantic models it relies on are
vendored under [`_vendor_v2/`](./_vendor_v2).

## At a glance

```
A2A message/send (user prompt)
    │
    ▼
CodingAgentExecutor          (coding_agent.py)
    │
    ▼
LLM planner loop             (agent_loop.py)
    tools:
      run_coder(task, files=None)              -> mini-swe-agent DefaultAgent
      discover_agents(skill_query)             -> AGNTCY Directory via dirctl
      send_email_via_directory(to, subject,
                               body, files=None)
                                               -> A2A email/send to discovered peer
    │
    ▼
structured_output {
  user_prompt, seeded_files,
  generated_files[],     # every file the runner created/modified
  delivery_results[],    # one entry per send_email_via_directory call
  agent {
    final_message, exit_reason, n_steps,
    elapsed_s, n_continuations,
    tool_calls[], run_coder_calls[],
    pending,             # null unless paused for confirmation
  }
}
```

The planner runs inside a per-request scratch workspace; the host filesystem
outside that workspace is never touched.

## Request contract

The only required input is the user's prompt (an A2A `TextPart`). All other
metadata is optional.

| Field | Where | Purpose |
|---|---|---|
| `metadata.source_files` | message metadata | List of `{"path", "content"}` dicts. Seeds the scratch workspace before the planner starts. `path` must be workspace-relative. |
| A2A `FilePart` parts | message parts | Alternative way to seed the workspace. `file.name` becomes the workspace-relative path; `file.bytes` is base64-decoded. |
| `contextId` | message | Pass an existing `contextId` to resume a paused conversation (see [Pause / resume](#pause--resume-on-email-confirmation)). |

There is no `metadata.skill` switch and no `metadata.delivery` block. The
planner reads the prose and chooses tools itself. Skill tags in the Agent
Card exist only for ADS discoverability.

## Response shape — `generated_files` + `delivery_results`

After the planner finishes, the diff between the seeded workspace and the
end-state is captured and shipped back inline:

```json
{
  "generated_files": [
    {
      "path": "src/foo.py",
      "action": "created",          // created | modified | deleted
      "size_bytes": 1234,
      "sha256": "...",
      "content": "...source code...",
      "content_truncated": false
    }
  ],
  "delivery_results": [             // one entry per send_email_via_directory call
    {
      "status": "sent",
      "to": ["alice@example.com"],
      "subject": "Generated docs",
      "agent_id": "email-agent",
      "agent_endpoint": "http://...",
    }
  ]
}
```

Files larger than 256 KB come back with `content_truncated: true` and no
inline `content`. Binary files are returned as base64 in `content_base64`.
Deleted seeded files are reported with `action: "deleted"`.

Common `delivery_results[*].status` values: `sent`, `no_files`,
`missing_recipient`, `directory_unavailable`, `discovery_failed`,
`no_agent_found`, `send_failed`, `send_rejected`, `input_required`
(see next section).

## Pause / resume on email confirmation

The coding agent does **not** auto-confirm email sends. The first
`send_email_via_directory(...)` call goes out without
`confirmation_status="confirmed"`; if the email agent returns
`input_required`, the planner pauses and the A2A response state becomes
`input_required` with the email agent's confirmation prompt as the message
body.

The next `message/send` carrying the **same `contextId`** is treated as the
user's reply: the executor reloads saved state from
`<workspace_root>/_state/<context_id>.json`, injects the user's text as the
tool result for the pending tool-call, and resumes. A "yes" typically
triggers a retried send with `confirmed=true`; a "no" yields a final
acknowledgment with the files in `generated_files` either way.

State is keyed by `contextId` (not `taskId`) because `a2a-sdk==0.3.2`
regenerates `taskId` per `message/send`. State is cleared and the workspace
removed on any terminal exit (`completed`, `step_limit`, `time_limit`,
crash); both are kept on `input_required` until the user resumes.

## YOLO mode

`mini-swe-agent`'s `DefaultAgent` has no human-in-the-loop confirmation;
every shell command the inner model emits runs immediately. The workspace
boundary contains the blast radius — the model's `cwd` is the per-request
scratch directory and the system prompt instructs it not to escape.

For safer deployment:

- Run inside a container, ephemeral VM, or CI runner.
- Point `--workspace-root` (or `CODING_AGENT_WORKSPACE_ROOT`) at a scratch
  volume.
- Keep step / cost limits conservative.
- The calling PI agent's policy gate still applies *before* the request
  reaches this server.

## ADS publish / unpublish (`dirctl`)

When `serve-a2a` is given `--directory-server <addr>` (or
`CODING_AGENT_DIRECTORY_URL` / `ADS_URL`), the coding agent:

1. On startup: converts its Agent Card to an ADS record (OASF schema 1.0.0)
   via the self-contained converter in `_ads_record.py`, writes it to a
   temp file, and runs:
   ```bash
   CID=$(dirctl --server-addr <addr> push <tmp> --output raw)
   dirctl --server-addr <addr> routing publish "$CID"
   ```
2. On shutdown (clean SIGINT/SIGTERM): runs
   `dirctl --server-addr <addr> routing unpublish "$CID"`. Hard kills
   (SIGKILL, OOM) skip this; periodically prune stale records.

The same dirctl is used by the outbound email-delivery hop, so configuring
`--directory-server` unlocks both halves in one go. Use `--no-publish` to
keep discovery on but skip self-registration.

## Outbound email delivery

When the planner calls `send_email_via_directory(to, subject, body, files=None)`:

1. The coding agent walks `dirctl routing list --output json`, pulls each
   A2A record, and matches `annotations.agentic_network.custom_skill_tags`
   against `email/send`.
2. The first match's `annotations.a2a.url` becomes the delivery endpoint.
3. The coding agent sends an A2A `email/send` request to that agent.
4. If `files` were specified, they are inlined into the email body as
   plaintext code blocks (`=== path (action, size) ===`) — today's email
   agent A2A boundary does not accept attachments.

## Setup

```bash
conda env create -f environment.yml
conda activate coding-agent-a2a
```

Set the planner model and provider credentials:

```bash
export CODING_AGENT_MODEL=anthropic/claude-sonnet-4-5-20250929   # litellm id (the inner runner)
export ANTHROPIC_API_KEY=sk-...
# Optional:
export CODING_AGENT_PLANNER_MODEL=...     # outer planner; defaults to CODING_AGENT_MODEL
export CODING_AGENT_WORKSPACE_ROOT=/var/scratch/coding-agent
export MSWEA_SILENT_STARTUP=1
export MSWEA_COST_TRACKING=ignore_errors
```

## Run

```bash
python coding_agent.py serve-a2a --host 0.0.0.0 --port 9106 --directory-server <IP address of ADS x.x.x.x>
```

Common flags:

| Flag | Purpose |
|---|---|
| `--card PATH` | Override the in-tree `coding_agent.card.json`. |
| `--directory-server ADDR` | ADS server for `dirctl` (publish + discovery). |
| `--no-publish` | Use `--directory-server` for discovery only. |
| `--dirctl-path PATH` | Path to the `dirctl` binary. Defaults to `PATH` lookup. |
| `--workspace-root PATH` | Parent dir for scratch workspaces. |
| `--keep-workspace` | Keep workspaces after the response (debugging). |
| `--log-level debug` | Per-step tool-call traces + seeded file paths. |
| `--allow-missing-model` | Skip the upfront `CODING_AGENT_MODEL` check (useful for tests with a fake runner). |

Before publishing, edit `coding_agent.card.json` (next to this README) and
set its `url` field to the deployment address.

## Test

Send a single request with the bundled client:

```bash
python send_request.py \
  --endpoint http://127.0.0.1:9106/ \
  --prompt "Write a Python script that checks if a number is prime, then email it to alice@example.com"
```

Useful `send_request.py` options:

| Option | Purpose |
|---|---|
| `--input-file HOST:WS` | Seed `WS` (workspace-relative path) with the contents of `HOST`. Repeatable. |
| `--save-files DIR` | Write returned `generated_files` (those with inline content) into `DIR`. |
| `--context-id ID` | Resume a paused conversation (see [Pause / resume](#pause--resume-on-email-confirmation)). |
| `--raw` | Dump the full JSON-RPC response instead of the summary. |
| `--timeout 900` | HTTP timeout in seconds. |

Resume a paused (email-confirmation) conversation:

```bash
python send_request.py \
  --endpoint http://127.0.0.1:9106/ \
  --context-id <id-from-previous-response> \
  --prompt "yes, please send it"
```

CLI mode (no A2A round-trip):

```bash
python coding_agent.py run-skill code/bug_fix \
  --prompt "Fix the failing test in tests/test_foo.py." \
  --input-file ./local_src/foo.py:src/foo.py \
  --input-file ./local_src/test_foo.py:tests/test_foo.py

python coding_agent.py tools   # list registered skills
```

## Running the test suite

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
PYTHONPATH=external_agents/coding_agent \
pytest external_agents/coding_agent/tests -q
```

The test suite injects fake runners and a fake directory; no LLM, no
network, no `dirctl` binary required.

## Directory layout

```
coding_agent/
├── coding_agent.py            # CLI + A2A executor
├── agent_loop.py              # LLM planner loop (tool_calls until terminal)
├── state_store.py             # context_id-keyed pause/resume state
├── _ads_record.py             # Agent Card -> OASF schema 1.0.0 (publish path)
├── _vendor_v2/                # vendored shared libs (no external dependency)
│   ├── common/models.py
│   ├── directory/{client,backends,card_oasf_converter}.py
│   └── a2a/{client,transport,adapters}.py
├── coding_agent.card.json     # Agent Card for ADS publication
├── send_request.py            # A2A test client
├── environment.yml            # conda env: coding-agent-a2a
└── tests/                     # pytest suite
```
