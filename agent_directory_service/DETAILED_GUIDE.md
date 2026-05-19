# Agent Directory Service Detailed Guide

This package is now ADS-only. It does not host the product main agent; use
`v3_main_agent` for that runtime. The files here are for operating the AGNTCY
directory service, publishing Agent Cards, checking routed records, and repairing
stale ADS routing state.

## Runtime Layout

- `scripts/manage_ads_lan.py`: starts/stops the local `dirctl` daemon and LAN
  proxy that exposes the shared ADS address.
- `configs/ads_daemon_fast_routing.yaml`: daemon config with short publication
  and indexer intervals so new publishes become routed quickly.
- `scripts/publish_agents.py`: validates Agent Cards and publishes them to ADS.
- `scripts/show_ads_routes.py`: resolves routed CIDs into names, endpoints,
  labels, and publication times.
- `scripts/ads_diagnostics.py`: compares routed CIDs, stored CIDs, and local
  publication state.
- `scripts/watch_ads_routing.py`: listens for ADS publish/unpublish events and
  retries missing route publication without restarting the daemon by default.
- `scripts/force_route_active_agents.py`: explicit manual repair tool for stale
  routing state.
- `services/directory/`: real ADS CLI/SDK backend plus Agent Card/OASF
  conversion helpers.
- `services/a2a/`: lightweight A2A client/adapters used only by endpoint
  validation.

## Install

From the repository root:

```bash
python3 -m venv agent_directory_service/.venv
agent_directory_service/.venv/bin/python -m pip install --upgrade pip
agent_directory_service/.venv/bin/python -m pip install -r agent_directory_service/requirements.txt
agent_directory_service/.venv/bin/python -m pip check
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.print_versions
```

`dirctl` must be installed separately and available on `PATH` for real ADS
operations:

```bash
command -v dirctl
```

## Start And Stop ADS

Start the shared LAN service:

```bash
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.manage_ads_lan \
  --listen-host 10.229.117.37 \
  --listen-port 8888 \
  start
```

Check status:

```bash
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.manage_ads_lan \
  --listen-host 10.229.117.37 \
  --listen-port 8888 \
  status
```

Stop the proxy and daemon:

```bash
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.manage_ads_lan \
  --listen-host 10.229.117.37 \
  --listen-port 8888 \
  stop --stop-daemon
```

## Publish And Inspect Agents

Publish one Agent Card:

```bash
AGENT_NETWORK_FAKE_ADS=false \
ADS_BACKEND=cli \
ADS_SERVER_ADDR=10.229.117.37:8888 \
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.publish_agents \
  --card path/to/agent.card.json
```

View routed agents with readable metadata:

```bash
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.show_ads_routes \
  --server-addr 10.229.117.37:8888
```

Diagnose stale records:

```bash
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.ads_diagnostics \
  --server-addr 10.229.117.37:8888
```

Repair active records explicitly:

```bash
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.force_route_active_agents \
  --server-addr 10.229.117.37:8888 \
  --restart-daemon-if-stuck \
  --delete-superseded
```

## Endpoint Validation

Validate an external A2A endpoint without publishing:

```bash
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.validate_agent_endpoint \
  --card path/to/agent.card.json \
  --skill email/current_time \
  --prompt "What time is it?" \
  --endpoint http://host:port/
```

Add `--publish --fake-ads` for local fake-directory validation, or set
`ADS_SERVER_ADDR` and omit `--fake-ads` to publish through real ADS.

## Test

Run the ADS package tests:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 agent_directory_service/.venv/bin/python -m pytest agent_directory_service/tests -q
```
