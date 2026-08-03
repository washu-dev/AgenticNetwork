"""Build the ADS-card carrier for an agent's declared resource requirements (Step 8).

Resource sizing (cpu/memory/concurrency/max_instances/gpu) rides on the published
card's ``capabilities.extensions`` for the same reason declared credentials do
(see ``card_credentials.py``): it is the one field the planner's discovery read
path round-trips. This is *visibility* for the planner/UI; the authoritative copy
the CloudRunBackend deploys from is the ``resources`` column on the agent row.

``AgentExtension.required`` is the A2A protocol-comprehension flag ("a client must
understand this extension to interact"), not a deploy directive, so it stays
``False`` — a strict A2A client never refuses to call the agent over sizing hints.
"""

from __future__ import annotations

from typing import Any

from a2a.types import AgentExtension

# Cross-component contract: the model shape mirrors models.ResourceRequirements.
RESOURCE_REQUIREMENTS_EXTENSION_URI = "agenticnetwork.dev/ext/resource-requirements"


def build_resources_extension(
    resources: dict[str, Any] | None,
) -> AgentExtension | None:
    """Return an extension carrying ``resources``, or ``None`` if unset.

    Omitting the extension when there are no declared resources keeps cards for
    agents that don't size themselves byte-identical to before Step 8.
    """
    if not resources:
        return None
    return AgentExtension(
        uri=RESOURCE_REQUIREMENTS_EXTENSION_URI,
        description="Deploy resource requirements this agent declares",
        required=False,
        params=dict(resources),
    )
