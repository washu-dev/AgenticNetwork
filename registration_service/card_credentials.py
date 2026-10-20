"""Build the ADS-card carrier for declared credentials (P4a).

Declared credentials ride on the published card's ``capabilities.extensions``
because that is the one field the planner's discovery read path round-trips
(ADS discovery discards ``annotations``). The planner reads this same URI back
(``agentnet/directory/card_oasf.py::_required_credentials_from_card``).

Contract note: ``AgentExtension.required`` is the A2A **protocol-comprehension**
flag ("a client must understand this extension to interact"), *not* whether a
user credential is required. We deliberately keep it ``False`` and drive
credential enforcement from ``params.credentials[].required`` in the planner
(P4c), so a strict A2A client never refuses to call the agent.
"""

from __future__ import annotations

from typing import Any

from a2a.types import AgentExtension

# Cross-component contract: the chat app's planner reads this URI back (P4b). It is
# the same string as agentnet.models.REQUIRED_CREDENTIALS_EXTENSION_URI, written out
# here because this module runs inside custom-agent processes, where an uploaded
# project's own top-level packages come first on the path (as container_runner does).
# tests/test_credentials.py checks the two stay equal.
CREDENTIALS_EXTENSION_URI = "agenticnetwork.dev/ext/required-credentials"


def build_credentials_extension(
    credentials: list[dict[str, Any]] | None,
) -> AgentExtension | None:
    """Return an extension carrying ``credentials``, or ``None`` if there are none.

    Omitting the extension when empty keeps cards for existing/other-type agents
    byte-identical to today (P4a decision A).
    """
    if not credentials:
        return None
    return AgentExtension(
        uri=CREDENTIALS_EXTENSION_URI,
        description="User credentials this agent consumes",
        required=False,
        params={"credentials": credentials},
    )
