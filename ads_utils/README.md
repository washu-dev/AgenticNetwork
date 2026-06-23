# ads_utils

Shared package for publishing and unpublishing agent cards to the AgenticNetwork Directory Service (ADS). Copied into every agent Docker image so each agent can self-register without duplicating the dirctl logic.

## Contents

| File | Purpose |
|---|---|
| `publisher.py` | `DirectoryPublisher` — wraps `dirctl push` + `dirctl routing publish`, polls until the record appears in the routing index |
| `converter.py` | Converts an A2A `AgentCard` to an ADS/OASF record; maintains the per-agent OASF routing skill map |

## Usage

```python
from ads_utils import DirectoryPublisher

pub = DirectoryPublisher(server_addr="localhost:8888")
record = pub.publish(card, advertise_url="http://localhost:9103/")
# record.backend_ref is the CID needed to unpublish later

pub.unpublish(record.backend_ref)
```

`DirectoryPublisher` reads `ADS_SERVER_ADDR` (or `ADS_URL`) from the environment if `server_addr` is not passed explicitly. It raises `DirectoryUnavailable` on any failure — callers should catch this and log a warning rather than crashing, since ADS may not be up yet at startup.

## Adding a new agent's OASF routing skills

`converter.py` maintains `_AGENT_OASF_ROUTING_SKILLS`, a dict keyed by lowercased hyphenated agent name (e.g. `"email-agent"`). Add an entry there for any new agent whose skill IDs don't already appear in `KNOWN_OASF_SKILLS`. Without a matching entry `DirectoryPublisher.publish()` will raise `ValueError`.

The registration service populates this map at runtime for dynamically registered agents via `_register_oasf_routing_skills()` in `supervisor.py`.

## Deployment

`deployment/dockerfiles/Dockerfile.*` copies this directory into `/app/ads_utils/` and sets `PYTHONPATH=/app`. No pip install required — it is imported directly as a local package.
