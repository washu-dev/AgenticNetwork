"""The Postgres address both services read: ``DATABASE_URL``, with the old name as a fallback.

The chat app and the registration service share one database, so they read its
address the same way, here. The old name ``REGISTRATION_DB_DSN`` still works, so
existing ``.env`` files and deployments keep running; using it is logged once as
deprecated.

The value is a URL asyncpg accepts: ``postgresql://`` or ``postgres://`` only
(``postgresql+asyncpg://`` and other SQLAlchemy forms are rejected). It holds the
password, so it is never logged.

Standard library only: both service venvs import it, and so do their images.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)

DATABASE_URL = "DATABASE_URL"
OLD_DATABASE_NAMES = ("REGISTRATION_DB_DSN",)  # renamed 2026-10-25

# Old names already warned about. The chat app reads the setting on every request,
# so each warning is logged once per process.
_warned: set[str] = set()


def database_url(env: Mapping[str, str] | None = None) -> str | None:
    """Return ``DATABASE_URL``, else the first old name set, else None.

    An empty value counts as unset (unlike ``chat_app.env.setting``), as the old
    ``bool(os.getenv(...))`` checks did: an empty ``DATABASE_URL`` must not hide a
    real old value, or the chat app would stop requiring login. When both are set
    to different values, ``DATABASE_URL`` wins. ``env`` defaults to ``os.environ``.
    """
    values = os.environ if env is None else env
    url = values.get(DATABASE_URL) or None
    for old in OLD_DATABASE_NAMES:
        old_url = values.get(old)
        if not old_url:
            continue
        if url is None:
            _warn_once(old, f"{old} is deprecated; use {DATABASE_URL}")
            return old_url
        if old_url != url:
            _warn_once(old, f"{DATABASE_URL} and {old} are both set, to different values; using {DATABASE_URL}")
    return url


def database_configured(env: Mapping[str, str] | None = None) -> bool:
    """Whether a database address is set (under the new name or an old one)."""
    return database_url(env) is not None


def reset_warnings() -> None:
    """Forget which warnings were logged (for tests)."""
    _warned.clear()


def _warn_once(old: str, message: str) -> None:
    if old in _warned:
        return
    _warned.add(old)
    logger.warning(message)
