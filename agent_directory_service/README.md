# Agent Directory Service (ADS)

ADS is the shared **agent registry** — the "phone book" where every agent
publishes a card so the others can discover it by skill. Agents publish their
card on startup and unpublish on shutdown; the planner queries ADS to find who
can do what.

## What this directory actually is

The **running ADS is just the AGNTCY `dirctl` daemon plus a config file** — none
of the Python in this directory ships inside it. In the cluster deploy
(`deployment/`), ADS is a container that runs
`dirctl daemon start --config deployment/ads_daemon.yaml` (Postgres-backed,
listening on `0.0.0.0:8888`), started first by `submit.sbatch` before any agent
comes up.

Everything in `agent_directory_service/` is therefore **operator and developer
tooling that runs *against* a live ADS** — from a laptop over an SSH tunnel, or
on the host — never as part of the deployed ADS runtime itself. It exists to
inspect, publish to, diagnose, and repair the directory.

### Why the repair tooling exists (the load-bearing subtlety)

An ADS record is both **stored** (content-addressed; `dirctl search`) *and*
**routed** (a separate routing index; `dirctl routing list/search`). A record can
be **stored but not routed**, in which case it is invisible to live discovery —
the planner never sees it. This is the most common failure mode, and most of the
`scripts/` suite exists to detect and fix it.

## Layout

```
agent_directory_service/
├── run_ads.sh          operator CLI — the front door; wraps dirctl + the scripts below
├── configs/
│   └── ads_daemon_fast_routing.yaml   local-dev dirctl daemon config (sqlite, fast routing intervals)
├── services/           importable library (used by scripts/ and tests/)
│   ├── directory/
│   │   ├── backends.py            Real/Fake ADS backends: dirctl push/pull/route + poll-until-routed
│   │   ├── card_oasf_converter.py Agent Card ↔ OASF/ADS record conversion
│   │   └── client.py              DirectoryClient — picks Fake vs Real backend (AGENT_NETWORK_FAKE_ADS)
│   ├── common/models.py           pydantic models (DiscoveredAgent, A2A task/message, PublishedRecord)
│   └── a2a/                       A2A call layer (used by validate_agent_endpoint)
├── scripts/            operator tools (each also reachable via run_ads.sh)
│   ├── show_ads_routes.py         -list    human-readable routed agents
│   ├── ads_diagnostics.py         -diag    classify routed / queued / stale / orphaned records
│   ├── watch_ads_routing.py       -watch   listen for publish events, re-route missing CIDs
│   ├── force_route_active_agents.py -repair route the latest record per agent, delete superseded
│   ├── publish_agents.py          -publish push an Agent Card to ADS
│   ├── validate_agent_endpoint.py test an A2A endpoint end-to-end without publishing
│   └── print_versions.py          dump installed dependency versions
└── tests/              pytest suite (run from the repo root)
```

> **Note:** `ads_diagnostics.py`'s record-state classification reads the local
> **sqlite** daemon DB, so its queued/stale detail only applies to a local
> (Tier-B) daemon. Against the cluster's **Postgres**-backed ADS it degrades to
> routing-list-vs-search.

## Agent Cards live with the agents, not here

Each agent's card (the source of truth for its skills and endpoint) lives next to
its code in the [`agent-directory`](https://github.com/washu-dev/agent-directory)
repo, e.g. `agent-directory/email_agent/email_agent.card.json`. Commands below
that take a `--card` expect a path to one of those files.

## Accessing ADS in the cluster

ADS listens on `localhost:8888` within the job's network. To reach it from your
laptop, open an SSH tunnel to the compute node while the job is running:

```bash
ssh -L 8888:<node>:8888 <user>@<login-node>
```

Then point `dirctl` or `run_ads.sh` at `localhost:8888` (the default).

## Inspect the live directory

```bash
agent_directory_service/run_ads.sh -list       # human-readable routed agents
agent_directory_service/run_ads.sh -raw-list   # raw dirctl routing list JSON
agent_directory_service/run_ads.sh -diag       # diagnose stored-but-not-routed records
agent_directory_service/run_ads.sh -watch      # watch publish/unpublish events, auto-repair missing routes
```

## Repair stale routing

```bash
agent_directory_service/run_ads.sh -repair          # route active stored records; no deletion
agent_directory_service/run_ads.sh -repair-delete   # route active records and delete superseded ones
agent_directory_service/run_ads.sh -remove <cid>    # remove a CID from routing only
agent_directory_service/run_ads.sh -delete <cid>    # unpublish then delete a CID from storage
```

## Manually publish an Agent Card

Use this when an agent crashed before it could publish, or to push a card update
without restarting the agent container:

```bash
agent_directory_service/run_ads.sh -publish ../agent-directory/email_agent/email_agent.card.json
```

## Validate an agent endpoint

Test an A2A endpoint directly, without publishing to ADS:

```bash
python -m agent_directory_service.scripts.validate_agent_endpoint \
  --card ../agent-directory/email_agent/email_agent.card.json \
  --skill email/current_time \
  --prompt "What time is it?" \
  --endpoint http://localhost:9103/
```

## Tests

```bash
# the registration venv matches the a2a-sdk 0.3.2 pin; dirctl is mocked
registration_service/.venv/bin/python -m pytest agent_directory_service/tests -q -p no:cacheprovider
```
