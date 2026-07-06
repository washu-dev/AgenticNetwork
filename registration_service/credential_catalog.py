"""Catalog of credential types a custom agent may declare (P4a).

This is the **single source of truth** for the canonical credential namespace
(Option A, locked 2026-07-02): the planner's per-user credential store will
mirror these ``type`` strings so that declaration → grant → injection all match
on the same key. See ``docs/AUTH.md`` §4 and ``docs/DECISIONS.md`` (D4).

Two shapes of ``type`` are accepted on a declaration:

* a catalog ``type`` (e.g. ``"openai_api_key"``) — a known credential the planner
  understands and can offer a first-class Settings entry for; or
* a free-form escape ``"custom:<slug>"`` — for a key the catalog does not know
  about yet. Treated as an ``api_key`` kind with a caller-supplied label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CredentialKind = Literal["api_key", "oauth"]

# ``custom:<slug>`` escape — lowercase letters/digits/-/_ after the prefix.
CUSTOM_TYPE_RE = re.compile(r"^custom:[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class CatalogEntry:
    type: str
    label: str
    kind: CredentialKind = "api_key"
    docs_url: str | None = None


# Ordered for display in the registration UI dropdown.
CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry("openai_api_key", "OpenAI API key", "api_key",
                 "https://platform.openai.com/api-keys"),
    CatalogEntry("anthropic_api_key", "Anthropic API key", "api_key",
                 "https://console.anthropic.com/settings/keys"),
    CatalogEntry("brave_api_key", "Brave Search API key", "api_key",
                 "https://brave.com/search/api/"),
    CatalogEntry("google_api_key", "Google API key", "api_key",
                 "https://console.cloud.google.com/apis/credentials"),
    CatalogEntry("google_maps_api_key", "Google Maps API key", "api_key",
                 "https://developers.google.com/maps/documentation/javascript/get-api-key"),
)

_BY_TYPE: dict[str, CatalogEntry] = {e.type: e for e in CATALOG}


def is_known_type(cred_type: str) -> bool:
    """True if ``cred_type`` is a catalog entry or a well-formed ``custom:<slug>``."""
    return cred_type in _BY_TYPE or bool(CUSTOM_TYPE_RE.match(cred_type))


def catalog_entry(cred_type: str) -> CatalogEntry | None:
    """Return the catalog entry for a known type, or ``None`` for ``custom:`` types."""
    return _BY_TYPE.get(cred_type)
