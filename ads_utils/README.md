# ads_utils

Shared package for publishing and unpublishing agent cards to the AgenticNetwork Directory Service (ADS). Copied into every agent Docker image so each agent can self-register without duplicating the dirctl logic.

## Contents

| File | Purpose |
|---|---|
| `publisher.py` | `DirectoryPublisher` — wraps `dirctl push` + `dirctl routing publish`, polls until the record appears in the routing index |
| `converter.py` | Converts an A2A `AgentCard` to an ADS/OASF record; resolves the ADS routing skill from an `oasf:<name>:<id>` card tag, falling back to a per-agent map |

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

Preferred: stamp an `oasf:<name>:<id>` tag (build it with `oasf_skill_tag(name, id)`) onto one of the card's skills before publishing — the card then self-describes its routing skill and `converter.py` needs no per-agent edit. This is what `registration_service` does: `_build_agent_card()` in `supervisor.py` and `generate_card_json()` in `builder.py` both stamp this tag from the agent's chosen `oasf_skills`.

Fallback: `converter.py` also maintains `_AGENT_OASF_ROUTING_SKILLS`, a dict keyed by lowercased hyphenated agent name (e.g. `"email-agent"`), for cards that carry no `oasf:` tag. Without a matching tag *or* map entry, `DirectoryPublisher.publish()` raises `ValueError`.

## Deployment

`deployment/dockerfiles/Dockerfile.*` copies this directory into `/app/ads_utils/` and sets `PYTHONPATH=/app`. No pip install required — it is imported directly as a local package.
