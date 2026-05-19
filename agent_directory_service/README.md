# Agent Directory Service

Agent Directory Service is the repo-local tooling for running, publishing to,
inspecting, and repairing the shared AGNTCY ADS directory. The product main
agent lives in `planner_agent`; PI runtime code is intentionally not part of
this package. This README is the short runbook; for more detail, see
[`DETAILED_GUIDE.md`](DETAILED_GUIDE.md).

## Install

Run from the repo root:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
python3 -m venv agent_directory_service/.venv
agent_directory_service/.venv/bin/python -m pip install --upgrade pip
agent_directory_service/.venv/bin/python -m pip install -r agent_directory_service/requirements.txt
agent_directory_service/.venv/bin/python -m pip check
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.print_versions
```

Conda alternative:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
conda env create -f agent_directory_service/environment.yml
conda activate agent-directory-service
```

`dirctl` must also be installed and available on `PATH` for real ADS mode:

```bash
command -v dirctl
```

## ADS Host

Use this on the machine hosting the shared ADS service. In the current LAN setup,
other agents should talk to:

```text
10.229.117.37:8888
```

Short wrapper for common operations:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork

# Start local dirctl daemon plus LAN proxy.
agent_directory_service/run_ads.sh -start

# Stop LAN proxy plus local dirctl daemon.
agent_directory_service/run_ads.sh -stop

# Show proxy and daemon status.
agent_directory_service/run_ads.sh -status

# Show routed agents with names, endpoints, labels, and publish times.
agent_directory_service/run_ads.sh -list

# Remove one routed CID from routing only.
agent_directory_service/run_ads.sh -remove <cid>

# Diagnose or repair stale routing.
agent_directory_service/run_ads.sh -diag
agent_directory_service/run_ads.sh -repair
```

Use `agent_directory_service/run_ads.sh -help` for the full wrapper command
list, including `-publish`, `-watch`, `-raw-list`, `-proxy-stop`, and `-delete`.

Start ADS with the managed LAN launcher:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.manage_ads_lan \
  --listen-host 10.229.117.37 \
  --listen-port 8888 \
  start
```

Check status:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.manage_ads_lan \
  --listen-host 10.229.117.37 \
  --listen-port 8888 \
  status
```

Stop the LAN proxy and the local `dirctl` daemon:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.manage_ads_lan \
  --listen-host 10.229.117.37 \
  --listen-port 8888 \
  stop --stop-daemon
```

The managed launcher uses `agent_directory_service/configs/ads_daemon_fast_routing.yaml`
by default, so published agents should be routed within seconds instead of
waiting for the default hourly ADS scheduler.

## Check Registered Agents

Raw ADS routing list:

```bash
dirctl --server-addr 10.229.117.37:8888 routing list --output json
```

Human-readable routing list with names, labels, endpoints, and publish times:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.show_ads_routes \
  --server-addr 10.229.117.37:8888
```

Diagnose stored-but-not-routed records:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.ads_diagnostics \
  --server-addr 10.229.117.37:8888
```

Manual repair path when ADS search shows records but routing is stale:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.force_route_active_agents \
  --server-addr 10.229.117.37:8888 \
  --restart-daemon-if-stuck \
  --delete-superseded
```

Optional watcher on the ADS host. It listens for publish/unpublish events and
repairs missing routed CIDs without restarting the daemon unless explicitly
requested:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.watch_ads_routing \
  --server-addr 10.229.117.37:8888
```

## Publish An Agent

Agents should publish Agent Cards to the shared ADS address:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=false \
ADS_BACKEND=cli \
ADS_SERVER_ADDR=10.229.117.37:8888 \
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.publish_agents \
  --card path/to/agent.card.json
```

For local-only testing on the ADS host, use `ADS_SERVER_ADDR=localhost:8888`
instead.

## Validate And Test

Validate one A2A endpoint without publishing:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.validate_agent_endpoint \
  --card path/to/agent.card.json \
  --skill email/current_time \
  --prompt "What time is it?" \
  --endpoint http://host:port/
```

Run the agent directory service tests:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 agent_directory_service/.venv/bin/python -m pytest agent_directory_service/tests -q
```

## Common Addresses

- Shared ADS for teammates: `10.229.117.37:8888`
- Local ADS on the host: `localhost:8888`
