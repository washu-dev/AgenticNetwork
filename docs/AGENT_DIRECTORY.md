# Agent Directory Service (ADS)

`agent_directory_service/` — the registry where agents publish a card so others discover them by
skill. It's operator tooling around the AGNTCY `dir` daemon (driven by the `dirctl` CLI). The
directory daemon listens on **:8888** (routing gossip on **:8999**).

The single most important thing to understand here is **stored ≠ routed**.

## Stored vs. routed (D1) — the load-bearing subtlety

A record has two independent states in `dir`:
- **Stored** — content-addressably pushed (`dirctl push` → a CID). Findable by `dirctl search`.
- **Routed** — present in the routing index (`dirctl routing publish <cid>`). Findable by
  `dirctl routing list/search`.

**Live discovery uses the routing index, not storage search.** So a record can be **stored but not
routed** — it exists, `dirctl search` finds it, yet it is **invisible to discovery**. This happens
when routing publish fails or the reconciler stalls.

The backend **fails closed** rather than hiding this: `search_agents` tries `routing search` →
`routing list`, and only consults storage `search` if `ADS_ALLOW_LOCAL_STORE_FALLBACK=true`. If
storage has matching records but the routing index is empty, it raises **`DirectoryUnavailable`**
("exists in storage but is not routed/discoverable") instead of silently returning the stored copy
(`services/directory/backends.py`). Diagnostics classify each CID as `healthy` (stored + routed),
`queued` (publish submitted, not yet indexed), `stale` (stored, was routed, now absent), or
`orphaned` (stored, no publication record).

Publish is therefore a **two-step + poll**: `push` → `routing publish` → poll `routing list --cid`
until the CID appears. The good publish paths (`ads_utils/publisher.py`, the planner backend,
`agent_skeleton/serve.py`) all poll; several per-agent inline publishers are fire-and-forget and can
leave an agent invisible with no error — check them when debugging discovery.

## Backends: real vs. fake

Both implement the same `DirectoryBackend` protocol (`publish_agent_card`, `publish_oasf_record`,
`search_agents`, `resolve_agent`, `healthcheck`):
- **`FakeDirectoryBackend`** — in-process, dict-backed, no `dirctl`, no routing index (everything
  published is instantly discoverable). Used for `--fake-ads` / offline / tests.
- **`RealADSDirectoryBackend`** — talks to `dir`. Two modes via `ADS_BACKEND`: **`cli`** (default,
  shells out to the `dirctl` binary at `DIRCTL_BIN`) or **`sdk`** (`agntcy.dir_sdk.client`, protobuf).
  Server address from `ADS_SERVER_ADDR`/`ADS_URL`/etc.

Selection is by env flag (`AGENT_NETWORK_FAKE_ADS`, and the planner's `--fake-ads`).

## `dirctl` commands the backends invoke
`push` (→ CID), `pull <cid>`, `routing publish <cid>`, `routing list [--cid|--skill]`,
`routing search [--skill]`, `search --skill` (storage fallback), `routing unpublish <cid>`,
`delete <cid>`, `routing info`, and `daemon start/stop/status`.

## Card ↔ OASF conversion (`card_oasf_converter.py`)

The bridge between an A2A `AgentCard` and a `dir` record:
- `agent_card_to_ads_record` — builds the ADS/OASF record (`schema_version 1.0.0`), embeds the
  original card under `modules[0].data.card_data`, and sets routing skills via
  `_ads_routing_skills_for_card` (mapping known skills to the official OASF taxonomy,
  `KNOWN_OASF_SKILLS`; demo-only skills stay as custom tags to avoid false routing hints). It also
  computes write-risk (`WRITE_SKILL_IDS`/`TERMS`) → `risk_level` + `write_requires_confirmation`.
- `oasf_record_to_discovered_agent` — the reverse, for search results (rebuilds the card, scores
  skills). The planner's fork additionally parses declared credentials off the card and accepts
  `demo-v2`/`demo-v3` records.
- **On-card routing tags** (`oasf:<name>:<id>`): a card can declare its own routing skills via a
  skill tag so generator-produced agents are discoverable without editing the hardcoded maps. This
  lives in `_ads_routing_skills_for_card` + `_oasf_skill_from_tag`. *Note:* this arrived with the
  agent-skeleton consolidation on `main` and lands here on that merge.

## Operator tooling

`run_ads.sh` dispatches the operator commands:

| Command | Does |
|---|---|
| `-list` | human-readable routed agents (name, endpoint, labels, times) |
| `-raw-list` | raw `dirctl routing list` JSON |
| `-diag` | classify every record healthy/queued/stale/orphaned |
| `-watch` | stream publish/unpublish events, auto-repair missing routes |
| `-repair` / `-repair-delete` | re-route the latest stored record per agent (optionally delete superseded) |
| `-publish <card>` | publish one Agent Card |
| `-remove <cid>` / `-delete <cid>` | unpublish from routing (and delete from storage) |

The `scripts/` suite backs these: `publish_agents.py`, `show_ads_routes.py`, `ads_diagnostics.py`,
`watch_ads_routing.py`, `force_route_active_agents.py` / `repair_ads_routing.py`, plus
`validate_agent_endpoint.py` (test an A2A endpoint without publishing) and `print_versions.py`.

The **fast-routing daemon config** (`configs/ads_daemon_fast_routing.yaml`) tightens the
publication scheduler + reconciler indexer intervals to ~5s so re-published records appear in
`routing list` quickly (the default intervals made freshly-repaired records linger as "queued").

## Caveats
- **Scout uses a different directory.** Every other agent uses `dirctl` at `:8888`; scout uses
  `agntcy_app_sdk` `AgentDirectory` at `DIRECTORY_ENDPOINT` (gRPC, default `:8889`). It's unverified
  whether these are the same logical directory — scout may not be discoverable by the others (see
  [`CLAUDE.md`](../CLAUDE.md) §9).
- **The converter/A2A stack is duplicated** across ~5 copies (canonical here, planner fork,
  `_vendor_v2/` in coding/intelligence, inline copies). A routing/policy fix in one may not
  propagate. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Key files
| Path | Role |
|---|---|
| `services/directory/backends.py` | real (`dirctl`) + fake backends, stored-vs-routed logic, `DirectoryUnavailable` |
| `services/directory/card_oasf_converter.py` | Card ↔ OASF record, routing-skill mapping |
| `run_ads.sh` | operator command dispatcher |
| `scripts/` | publish / show / diagnose / repair / watch / validate |
| `configs/ads_daemon_fast_routing.yaml` | fast-routing daemon tuning |
