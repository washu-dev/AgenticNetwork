# AgenticNetwork

This repository contains the current AgenticNetwork components:

- `agent_directory_service/`: AGNTCY ADS operator tooling for starting the
  shared directory service, publishing Agent Cards, inspecting routes, and
  repairing stale routing state.
- `planner_agent/`: product main agent runtime.
- Root-level specialized agents such as `email_agent/`, `coding_agent/`,
  `calendar_agent/`, `disaster_response_agent/`, `geoSpatial_agent/`, and
  `intelligence_agent/`.

The old prototype and milestone scaffolding have been removed from the active
tree. Start with each component README for setup and run commands.

## ADS Quick Start

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
python3 -m venv agent_directory_service/.venv
agent_directory_service/.venv/bin/python -m pip install --upgrade pip
agent_directory_service/.venv/bin/python -m pip install -r agent_directory_service/requirements.txt
agent_directory_service/.venv/bin/python -m pip check
```

Start the shared ADS host:

```bash
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.manage_ads_lan \
  --listen-host 10.229.117.37 \
  --listen-port 8888 \
  start
```

List routed agents:

```bash
agent_directory_service/.venv/bin/python -m agent_directory_service.scripts.show_ads_routes \
  --server-addr 10.229.117.37:8888
```

See `agent_directory_service/README.md` for the full ADS runbook.
