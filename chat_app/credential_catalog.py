"""Canonical credential types (the chat app's mirror of the registration catalog).

The registration service owns the authoritative catalog
(``registration_service/credential_catalog.py``). The chat app is a separate
deploy unit and cannot import it, so the canonical ``type`` values are mirrored
here. Keep the two in sync when adding a type. (P4c, Option A namespace.)

A credential ``type`` is valid if it is a known catalog type OR a well-formed
``custom:<slug>`` escape.
"""

from __future__ import annotations

import re

CUSTOM_TYPE_RE = re.compile(r"^custom:[a-z0-9][a-z0-9_-]*$")

# Mirror of registration_service.credential_catalog CATALOG type values.
KNOWN_TYPES: frozenset[str] = frozenset({
    "openai_api_key",
    "anthropic_api_key",
    "brave_api_key",
    "google_api_key",
    "google_maps_api_key",
})


def is_known_type(cred_type: str) -> bool:
    """True if ``cred_type`` is a catalog type or a well-formed ``custom:<slug>``."""
    return cred_type in KNOWN_TYPES or bool(CUSTOM_TYPE_RE.match(cred_type))
