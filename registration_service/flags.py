"""Boolean environment flags, parsed the same way everywhere they are read.

FAKE_DB used to be tested with a bare ``os.getenv("FAKE_DB")``, so any non-empty
value — including ``FAKE_DB=0`` — switched fake mode ON, while the admin bypass
required exactly ``"1"``. Every reader now goes through ``env_flag``: only
1/true/yes/on (any case) are true; unset or empty falls back to the default.
"""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return raw.lower() in _TRUTHY


def fake_db_enabled() -> bool:
    """In-memory store (fake_db.py) instead of postgres."""
    return env_flag("FAKE_DB")


def fake_supervisor_enabled() -> bool:
    """No-op supervisor (no agent processes, no directory publish).

    Defaults to FAKE_DB, so ``FAKE_DB=1`` alone keeps the long-standing no-spawn
    dev/test mode. ``FAKE_DB=1 FAKE_SUPERVISOR=0`` is the native dev stack: the
    in-memory store with real subprocess agents published through dirctl.
    """
    return env_flag("FAKE_SUPERVISOR", default=fake_db_enabled())
