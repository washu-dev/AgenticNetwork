# agentnet

The one shared library for talking to the directory (ADS) and to agents over A2A.
The chat app and the registration service both use it. In the chat app, the planner
(`chat_app/planner/`) searches the directory and calls agents with it, and the web
server (`chat_app/server.py`) lists agents, checks their health and removes them
(admin) with it. The registration service publishes and unpublishes the agents it
runs with it. Both read the database address through `settings.py`, the signed-in
user and the admin list through `identity.py`, and the credential types through
`credential_catalog.py`. agentnet is a library, not a service: it runs inside those
two. It also holds the operator scripts for inspecting and repairing a live directory.

## The directory (ADS)

ADS is the shared **agent registry**: the "phone book" where every agent
publishes a card so the others can discover it by skill. Agents get published when
they start and unpublished when they stop; the planner queries ADS to find who can
do what.

The **running ADS is just the AGNTCY `dirctl` daemon plus a config file**. None
of the Python here ships inside it. In the cluster deploy (`deployment/`), ADS is
a container that runs `dirctl daemon start --config deployment/ads_daemon.yaml`
(Postgres-backed, listening on `0.0.0.0:8888`), started first by `submit.sbatch`
before any agent comes up.

So the scripts in `agentnet/scripts/` are **operator and developer tooling that
runs *against* a live ADS**, from a laptop over an SSH tunnel or on the host. They
inspect, publish to, diagnose and repair the directory.

### Why the repair tooling exists (the load-bearing subtlety)

An ADS record is both **stored** (content-addressed; `dirctl search`) *and*
**routed** (a separate routing index; `dirctl routing list/search`). A record can
be **stored but not routed**, in which case it is invisible to live discovery:
the planner never sees it. This is the most common failure mode, and most of the
`scripts/` suite exists to detect and fix it.

## Layout

```
agentnet/
├── models.py              wire models (DiscoveredAgent, A2A task/message/part, PublishedRecord)
│                          and the required-credentials extension URI
├── settings.py            the database address both services read: DATABASE_URL, falling back
│                          to the old REGISTRATION_DB_DSN, and the PG* variables that can also
│                          name it (standard library only)
├── identity.py            the SSO identity headers (mail, then eppn, then remote-user) and the
│                          ADMIN_EMAILS allowlist (standard library only)
├── credential_catalog.py  the credential types agents declare and users store: the catalog
│                          and custom:<slug> (standard library only)
├── directory/
│   ├── __init__.py        DirectoryUnavailable (one class, raised by everything below)
│   ├── card_oasf.py       Agent Card <-> OASF/ADS record conversion, oasf_skill_tag
│   ├── backends.py        Real/Fake ADS backends: dirctl push/pull/route + poll-until-routed
│   ├── client.py          DirectoryClient: picks Fake vs Real backend (AGENT_NETWORK_FAKE_ADS)
│   └── publisher.py       DirectoryPublisher: the registration service's publish/unpublish
├── a2a/
│   ├── adapters.py        internal models <-> a2a-sdk types
│   ├── transport.py       HTTP A2A transport (streaming, Cloud Run ID-token auth for *.run.app)
│   └── client.py          A2AClient
├── scripts/               operator tools (each also reachable via scripts/ads)
│   ├── show_ads_routes.py         -list    human-readable routed agents
│   ├── ads_diagnostics.py         -diag    classify routed / queued / stale / orphaned records
│   ├── watch_ads_routing.py       -watch   listen for publish events, re-route missing CIDs
│   ├── force_route_active_agents.py -repair route the latest record per agent, delete superseded
│   ├── publish_agents.py          -publish push an Agent Card to ADS
│   ├── validate_agent_endpoint.py test an A2A endpoint end-to-end without publishing
│   └── print_versions.py          dump installed dependency versions
├── tests/                 pytest suite (run from the repo root)
└── requirements-sdk.txt   optional: only for ADS_BACKEND=sdk
```

Related files outside this folder: `scripts/ads` (the operator command, below) and
`deployment/ads_daemon.local.yaml` (a local-dev dirctl daemon config with sqlite and
fast routing intervals; `-watch --restart-daemon-if-stuck` restarts the daemon with
it). A daemon restarted by a repair writes its output to `logs/ads_daemon_repair.log`.

agentnet has no required dependencies file of its own (requirements-sdk.txt is only for the
optional SDK backend): it runs in the repo's one dev venv (`requirements-dev.txt`;
both services pin a2a-sdk 0.3.2), and CI runs its tests on Python 3.11 and 3.13.
Both Docker images copy it to `/app/agentnet` and set
`PYTHONPATH=/app`. Agents the registration service starts as local subprocesses
import `credential_catalog.py` too (through `registration_service/models.py`), with
the uploaded project first on their path, so an upload may not carry a top-level
`agentnet` module or package (`agentnet/`, `agentnet.py`, or an `entry_file` of that
name; `registration_service/archive.py`). The agent images built for
the cluster and Cloud Run do not include agentnet, and `agent_skeleton` never imports it.

> **Note:** `ads_diagnostics.py`'s record-state classification reads the local
> **sqlite** daemon DB, so its queued/stale detail only applies to a local
> (Tier-B) daemon. Against the cluster's **Postgres**-backed ADS it degrades to
> routing-list-vs-search.

## Publishing from code (`DirectoryPublisher`)

```python
from agentnet.directory.publisher import DirectoryPublisher

pub = DirectoryPublisher(server_addr="localhost:8888")
record = pub.publish(card, advertise_url="http://localhost:9103/")
# record.backend_ref is the CID needed to unpublish later

pub.unpublish(record.backend_ref)
```

`DirectoryPublisher` reads `ADS_SERVER_ADDR` (or `ADS_URL`) from the environment if
`server_addr` is not passed, and runs the `dirctl` binary named by `DIRCTL_BIN`
(default `dirctl` on `PATH`). It raises `DirectoryUnavailable` on any directory
failure. Callers should catch it and log a warning rather than crash, since ADS may
not be up yet at startup. `unpublish` does not raise: it returns `True` once the
record is no longer routed, including when the directory does not have the CID at
all (dirctl's "record not found", e.g. after a directory reset), and `False` on any
other failure, so the caller keeps the CID and retries.

A card declares its own routing skill with an `oasf:<name>:<id>` tag (build it with
`oasf_skill_tag(name, id)` from `directory/card_oasf.py`) on one of its skills. The
registration service stamps this tag from the agent's chosen `oasf_skills`
(`_build_agent_card()` in `supervisor.py`, `generate_card_json()` in `builder.py`).
A card with no `oasf:` tag makes `publish()` raise `ValueError`. Only the skill's id
is sent to ADS; ADS looks up the name.

The planner's read path and `publish_agents.py` also use a small hard-coded routing
map for older cards that carry no `oasf:` tag (`KNOWN_OASF_SKILLS` and the email
agent's skills in `card_oasf.py`). The publisher does not.

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

Then point `dirctl` or `scripts/ads` at `localhost:8888` (the default).

`scripts/ads` runs the Python tools with the dev venv (`.venv`). Set `ADS_PYTHON` to
use another interpreter.

## Inspect the live directory

```bash
scripts/ads -list       # human-readable routed agents
scripts/ads -raw-list   # raw dirctl routing list JSON
scripts/ads -diag       # diagnose stored-but-not-routed records
scripts/ads -watch      # watch publish/unpublish events, auto-repair missing routes
```

## Repair stale routing

```bash
scripts/ads -repair          # route active stored records; no deletion
scripts/ads -repair-delete   # route active records and delete superseded ones
scripts/ads -remove <cid>    # remove a CID from routing only
scripts/ads -delete <cid>    # unpublish then delete a CID from storage
```

## Manually publish an Agent Card

Use this when an agent crashed before it could publish, or to push a card update
without restarting the agent container:

```bash
scripts/ads -publish ../agent-directory/email_agent/email_agent.card.json
```

## Validate an agent endpoint

Test an A2A endpoint directly, without publishing to ADS:

```bash
.venv/bin/python -m agentnet.scripts.validate_agent_endpoint \
  --card ../agent-directory/email_agent/email_agent.card.json \
  --skill email/current_time \
  --prompt "What time is it?" \
  --endpoint http://localhost:9103/
```

## Tests

```bash
# dirctl is mocked; from the repo root, in the dev venv
scripts/stack test agentnet
# the same as: .venv/bin/python -m pytest agentnet/tests -q -p no:cacheprovider
```
