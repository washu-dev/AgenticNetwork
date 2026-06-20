# Agent Directory Service

ADS is the shared agent registry. It runs as a container in the ENGR cluster
deployment, started first by `submit.sbatch` before any agent container comes up.
Each agent waits for ADS on port 8888, publishes its card on startup, and
unpublishes on shutdown. The planner queries ADS to discover available agents.

Agent Cards (the source of truth for each agent's skills and endpoint) live next
to the agent code — not in this directory:

```
email_agent/email_agent.card.json
coding_agent/coding_agent.card.json
disaster_response_agent/disaster_response_agent.card.json
intelligence_agent/intelligence_agent.card.json
geoSpatial_agent/geospatial_agent.card.json
```

## Accessing ADS in the Cluster

ADS runs on `localhost:8888` within the job's network. To reach it from your
laptop, open an SSH tunnel to the compute node while the job is running:

```bash
ssh -L 8888:<node>:8888 <user>@<login-node>
```

Then point `dirctl` or `run_ads.sh` at `localhost:8888` (the default).

## Inspect the Live Directory

Human-readable list of routed agents:

```bash
agent_directory_service/run_ads.sh -list
```

Raw JSON:

```bash
agent_directory_service/run_ads.sh -raw-list
```

Diagnose stored-but-not-routed records (the most common failure mode — an agent
that published to storage but whose routing entry is missing):

```bash
agent_directory_service/run_ads.sh -diag
```

Watch for publish/unpublish events and auto-repair missing routes:

```bash
agent_directory_service/run_ads.sh -watch
```

## Repair Stale Routing

Route all active stored records without deleting anything:

```bash
agent_directory_service/run_ads.sh -repair
```

Route active records and delete superseded ones:

```bash
agent_directory_service/run_ads.sh -repair-delete
```

Remove a specific CID from routing only:

```bash
agent_directory_service/run_ads.sh -remove <cid>
```

Delete a CID from routing and storage:

```bash
agent_directory_service/run_ads.sh -delete <cid>
```

## Manually Publish an Agent Card

Use this when an agent crashed before it could publish, or to push a card update
without restarting the agent container:

```bash
agent_directory_service/run_ads.sh -publish email_agent/email_agent.card.json
```

## Validate an Agent Endpoint

Test an A2A endpoint directly without publishing to ADS:

```bash
python -m agent_directory_service.scripts.validate_agent_endpoint \
  --card email_agent/email_agent.card.json \
  --skill email/current_time \
  --prompt "What time is it?" \
  --endpoint http://localhost:9103/
```

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest agent_directory_service/tests -q
```
