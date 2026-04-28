# V2 Agent Network

This directory contains the new AGNTCY-based PI-agent network. Milestones 1-2
implement only the Weather path: fake Directory discovery, A2A SDK adapters,
HTTP A2A transport, a mock Weather A2A server, and a PI HTTP fallback endpoint.

This is not the v1 image-classification demo.

## OASF Skill Handling

Agent Cards describe how to interact with A2A servers. OASF/ADS records describe
how agents are published and discovered. The converter keeps these concepts
separate.

The Weather demo skills such as `weather/forecast` are stored as custom
demo-specific skill tags because no local official OASF taxonomy entry is known
for them in this repository. The fake DirectoryClient searches both official
OASF skills and custom skill tags, but it does not pretend custom tags are
official OASF taxonomy entries.

## Milestone 2 Demo

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
AGENT_NETWORK_FAKE_ADS=true v1_classification/.venv/bin/python -m v2_agent_network.scripts.run_weather_demo
```

Run the Milestone 1-2 tests with pytest plugin autoload disabled. This avoids
loading unrelated system plugins from the ROS environment.

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 v1_classification/.venv/bin/python -m pytest v2_agent_network/tests -q
```

Print the dependency report:

```bash
cd /home/cspl-yi/Downloads/agentic_network/AgenticNetwork
v1_classification/.venv/bin/python -m v2_agent_network.scripts.print_versions
```

Known local versions:

- `a2a-sdk==0.3.2`
- `agntcy-app-sdk==0.4.6`
- `agntcy-dir==1.2.1`
- `protobuf==6.33.6`
- `pydantic==2.12.5`
- `fastapi==0.136.1`

Compatibility notes:

- The A2A boundary uses installed `a2a-sdk` models. A new `message/send` request
  omits `message.task_id`; in `a2a-sdk==0.3.2`, setting it means "continue an
  existing task" and the server rejects unknown task IDs.
- The installed SDK's `a2a.client.A2AClient` is deprecated, so later milestones
  should move to the SDK's `ClientFactory` JSON-RPC path if the project upgrades
  the A2A client layer.
- Real ADS is not implemented in Milestones 1-2. `DirectoryClient` raises unless
  `AGENT_NETWORK_FAKE_ADS=true` or `DirectoryClient(fake_ads=True)` is used.

Later milestones will add the real ADS backend, more mock agents, Telegram and
WhatsApp adapters, observability, Docker Compose, and SLIM/NATS transports.
