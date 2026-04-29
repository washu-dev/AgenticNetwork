# Coding Agent

This directory contains the standalone `mini-swe-agent`-backed Coding Agent
service. It is intended to be operated as an external organization-owned A2A
agent and discovered by the AgenticNetwork through its Agent Card.

## What It Does

- Inspects source files supplied in the request and proposes the next coding
  task.
- Reviews supplied source and reports findings.
- Drafts module documentation and writes it as files in the response.
- Prepares bug-fix and test-generation patches and ships the modified files
  back to the caller (PI gates these skills with confirmation before reaching
  this server).
- Exposes those capabilities through an A2A HTTP endpoint.

The actual work is performed by `mini-swe-agent`'s `DefaultAgent`, which runs
an LM-driven shell loop. Every request runs inside its own scratch workspace
so the host filesystem outside that workspace is never touched.

## Workspace & File Return Contract

The coding agent is a **remote service**, not a code editor running on the
caller's machine. It accepts source files in the A2A request, runs in a
sandbox, and returns the resulting source files in the response.

### Input — seeding the workspace

Two flavours of source input are accepted on the request:

* `metadata.source_files = [{"path": "src/foo.py", "content": "..."}]` — a
  simple inline list. `path` must be a workspace-relative path (no `..`,
  no absolute paths).
* A2A native `FilePart` parts on the message, with `file.bytes` (base64) and
  `file.name` set to the workspace-relative path.

### Output — generated files

After the runner finishes, every file the model created or modified inside
the workspace is captured and shipped back inline on the response artifact's
`structured_output.generated_files`:

```json
[
  {
    "path": "src/foo.py",
    "action": "created",
    "size_bytes": 1234,
    "sha256": "...",
    "content": "...source code...",
    "content_truncated": false
  }
]
```

Files larger than 256 KB are returned with `content_truncated: true` and
no inline `content` (caller can request them by URI in a follow-up — TODO).
Binary files are returned as base64 in `content_base64`. Deleted seeded
files are also reported (`action: "deleted"`).

The scratch workspace is removed once the response is sent. Pass
`--keep-workspace` to retain it for debugging.

### ADS publish / unpublish (via `dirctl`)

When the `serve-a2a` command is given `--directory-server <addr>` (or
`CODING_AGENT_DIRECTORY_URL` / `ADS_URL` env vars), the coding agent:

* On startup, converts its Agent Card to an ADS record (schema 1.0.0)
  via the self-contained converter in `_ads_record.py` (no dependency on
  the AgenticNetwork repo at runtime), writes it to a temp file, and
  runs:

  ```bash
  CID=$(dirctl --server-addr <addr> push <tmp> --output raw)
  dirctl --server-addr <addr> routing publish "$CID"
  ```

  …caching the CID for cleanup.

* On shutdown (uvicorn returns from SIGINT/SIGTERM), runs:

  ```bash
  dirctl --server-addr <addr> routing unpublish "$CID"
  ```

  …so the agent stops appearing in directory routing the moment the
  process exits cleanly. Hard kills (SIGKILL, OOM) skip this step;
  operators should periodically prune stale records.

Required: `dirctl` must be on the path that the coding agent runs in (or
pass `--dirctl-path /path/to/dirctl`). The same dirctl is also used by the
delivery hop below for discovery, so configuring `--directory-server`
unlocks both halves of the integration in one go.

Pass `--no-publish` to use `--directory-server` for *discovery only* (e.g.
when a sidecar handles publishing for you).

### Optional — email delivery via a discovered agent

If the request includes a `delivery` block in the metadata, the coding agent
discovers an email-capable agent through the AGNTCY Directory and ships the
generated files to a recipient *before* returning. The result of the
delivery attempt is reported in `structured_output.delivery_result`:

```json
{
  "skill": "code/documentation",
  "delivery": {
    "kind": "email",
    "to": ["alice@example.com"],
    "cc": [],
    "bcc": [],
    "subject": "Generated docs",
    "body": "Here's what you asked for.",
    "skill_query": "email/send",
    "directory_url": "http://your-ads-host:port",
    "fake_ads": false
  }
}
```

Discovery configuration (in priority order):

* `delivery.directory_url` on the request, or
* `CODING_AGENT_DIRECTORY_URL` env var, or
* `ADS_URL` env var.

Set `delivery.fake_ads: true` (or `AGENT_NETWORK_FAKE_ADS=true`) to use the
in-process fake directory for tests. Real ADS lookups shell out to the
`dirctl` CLI (the Python `agntcy-dir` SDK is currently incompatible with the
project's pinned `protobuf`). The coding agent runs:

```bash
dirctl --server-addr <addr> routing list --output json
dirctl --server-addr <addr> pull <cid> --output json     # for each A2A record
```

…and matches `annotations.agentic_network.custom_skill_tags` (and
`modules[].data.card_data.skills[].id` for non-AgenticNetwork agents) against
`skill_query`. The first match's `annotations.a2a.url` becomes the delivery
endpoint.

Discovery returns the first agent registered for `skill_query` (default:
`email/send`). The coding agent then sends an A2A `email/send` request to
that agent, marked `confirmation_status: "confirmed"` (the caller's opt-in to
the `delivery` block constitutes consent for this server-to-server hop).

**Attachment caveat**: today's email agent A2A boundary does not accept
attachments — the coding agent inlines generated files into the email body
as plaintext code blocks (`=== path (action, size) ===`). To deliver real
attachments, the email agent's A2A schema would need an `attachments` field;
that change would live in the email agent, not here.

`structured_output.delivery_result.status` values:

| status | meaning |
|--------|---------|
| `sent` | The email agent accepted the send and acknowledged it. |
| `no_files` | The runner produced no files to deliver — delivery skipped. |
| `missing_recipient` | The `delivery.to` field was empty. |
| `unsupported_kind` | `delivery.kind` was not `"email"`. |
| `directory_unavailable` | The Directory client failed to construct (real ADS misconfigured, etc.). |
| `discovery_failed` | The Directory client raised while searching. |
| `no_agent_found` | The Directory has no agent registered for `skill_query`. |
| `send_failed` | The A2A call to the email agent raised. |
| `send_rejected` | The email agent returned a non-completed task state. |

## YOLO Mode

This wrapper runs `mini-swe-agent` in **yolo mode**: every bash command emitted
by the model is executed by `LocalEnvironment` immediately, without any
human-in-the-loop confirmation. This is intentional — when another agent calls
the coding agent over A2A there is no operator at the keyboard to approve
commands.

The workspace boundary contains the blast radius. The LM's `cwd` is the
per-request scratch directory and the prompt instructs the model not to
escape it. To deploy more safely:

- Run the service inside a container, dedicated VM, or ephemeral CI runner.
- Point `--workspace-root` (or `CODING_AGENT_WORKSPACE_ROOT`) at a path
  on a scratch volume.
- Keep the read-only and write-capable step / cost limits conservative.
- The PI agent's policy still gates `code/bug_fix` and `code/test_generation`
  with `requires_confirmation` *before* the request reaches this server.

## Setup

Create the conda environment:

```bash
conda env create -f environment.yml
conda activate coding-agent-a2a
```

Set the model and provider credentials:

```bash
export CODING_AGENT_MODEL=anthropic/claude-sonnet-4-5-20250929
export ANTHROPIC_API_KEY=sk-...
# Optional:
export CODING_AGENT_WORKSPACE_ROOT=/var/scratch/coding-agent
export MSWEA_SILENT_STARTUP=1
export MSWEA_COST_TRACKING=ignore_errors
```

## Run

Start the external Coding Agent service:

```bash
conda activate coding-agent-a2a
python coding_agent.py serve-a2a --host 0.0.0.0 --port 9106
```

The Agent Card should point to the reachable host and port, for example:

```text
http://127.0.0.1:9106/
```

Update `AgenticNetwork/v2_agent_network/configs/agent_cards/coding_agent.card.json`'s
`url` field to match the deployment address before publishing.

## Test

From the AgenticNetwork repo, validate the running endpoint (no source input):

```bash
conda activate coding-agent-a2a
python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/coding_agent.card.json \
  --skill code/documentation \
  --prompt "Draft a README.md for a tiny Python package called greeter that exports hello()." \
  --endpoint http://127.0.0.1:9106/
```

Run a single skill from the CLI without an A2A round-trip, seeding the
workspace from local files:

```bash
python coding_agent.py run-skill code/bug_fix \
  --prompt "Fix the failing test in tests/test_foo.py." \
  --input-file ./local_src/foo.py:src/foo.py \
  --input-file ./local_src/test_foo.py:tests/test_foo.py
```

The CLI prints the captured `generated_files` along with the runner metadata.

List the registered skills:

```bash
python coding_agent.py tools
```

Write actions (`code/bug_fix`, `code/test_generation`) require confirmation
metadata in the A2A request. PI's policy layer adds that gate automatically.
