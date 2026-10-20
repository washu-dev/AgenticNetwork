"""Setting names for the chat app and its planner, with the old names as fallbacks.

Settings only this service reads use two prefixes: ``PLANNER_*`` for the planner
loop (the LLM loop that picks agents) and ``CHAT_APP_*`` for the web server around
it. The old names (mostly ``MAIN_AGENT_*``) still work, so existing ``.env`` files
and deployments keep running; each old name in use is logged once as deprecated.
Settings other services share (``REGISTRATION_DB_DSN``, ``ADMIN_EMAILS``,
``OPENAI_*``, ``ADS_*`` and so on) keep their names and are not read here.

This module imports nothing from the package, so every layer can read settings
through it: the app (server.py, __main__.py), the planner (planner/config.py,
planner/runtime.py) and the store.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

# New name -> the old names it replaces, checked in order. Only these exact names
# are read; never scan a prefix (other PLANNER_* variables exist that are not
# settings of this service, e.g. PLANNER_IMAGE_TAG and PLANNER_INVOKER_SA).
RENAMED: dict[str, tuple[str, ...]] = {
    # Planner loop
    "PLANNER_LLM_ENABLED": ("MAIN_AGENT_LLM_ENABLED",),
    "PLANNER_LLM_PROVIDER": ("MAIN_AGENT_LLM_PROVIDER",),
    "PLANNER_LLM_MODEL": ("MAIN_AGENT_LLM_MODEL",),
    "PLANNER_LLM_MAX_OUTPUT_TOKENS": ("MAIN_AGENT_MAX_OUTPUT_TOKENS",),
    "PLANNER_LLM_TIMEOUT_SECONDS": ("MAIN_AGENT_TIMEOUT_SECONDS",),
    "PLANNER_LLM_PRIMARY_TIMEOUT_SECONDS": ("MAIN_AGENT_LLM_PRIMARY_TIMEOUT_SECONDS",),
    "PLANNER_LLM_FALLBACK_PROVIDER": ("MAIN_AGENT_LLM_FALLBACK_PROVIDER",),
    "PLANNER_LLM_FALLBACK_BASE_URL": ("MAIN_AGENT_LLM_FALLBACK_BASE_URL",),
    "PLANNER_LLM_FALLBACK_MODEL": ("MAIN_AGENT_LLM_FALLBACK_MODEL",),
    "PLANNER_MAX_STEPS": ("MAIN_AGENT_MAX_STEPS",),
    "PLANNER_HISTORY_LIMIT": ("MAIN_AGENT_HISTORY_LIMIT",),
    "PLANNER_POLICY_MODE": ("MAIN_AGENT_POLICY_MODE",),
    # Chat app (web server)
    "CHAT_APP_HOST": ("MAIN_AGENT_HOST",),
    "CHAT_APP_PORT": ("MAIN_AGENT_PORT",),
    "CHAT_APP_LOG_LEVEL": ("MAIN_AGENT_LOG_LEVEL",),
    "CHAT_APP_UPLOAD_DIR": ("MAIN_AGENT_UPLOAD_DIR",),
    "CHAT_APP_MAX_UPLOAD_BYTES": ("MAIN_AGENT_MAX_UPLOAD_BYTES",),
    "CHAT_APP_CARD_PATHS": ("MAIN_AGENT_CARD_PATHS",),
    "CHAT_APP_ALLOW_ANON": ("PLANNER_ALLOW_ANON",),
    "CHAT_APP_HEALTH_PROBE_SKIP_AGENTS": ("HEALTH_PROBE_SKIP_AGENTS",),
}

DEFAULT_UPLOAD_DIR = "/tmp/chat_app_uploads"

# Old-name notices, keyed by old name. Some settings are read before logging is
# configured (__main__.py reads host and port first), so notices wait in _pending
# until log_old_names() runs; after that, a new notice is logged when it happens.
_pending: dict[str, str] = {}
_logged: set[str] = set()
_log_at_once = False


def setting(name: str, default: str | None = None, env: Mapping[str, str] | None = None) -> str | None:
    """Return setting ``name`` (a new name in RENAMED), else the first old name set, else ``default``.

    A name counts as set when it is present, even if empty, as with ``os.getenv``.
    When both are set to different values, the new name wins. ``env`` defaults to
    ``os.environ``.
    """
    values = os.environ if env is None else env
    old_names = RENAMED[name]
    if name in values:
        value = values[name]
        for old in old_names:
            if old in values and values[old] != value:
                _notice(old, f"{name} and {old} are both set, to different values; using {name}")
        return value
    for old in old_names:
        if old in values:
            _notice(old, f"{old} is deprecated; use {name}")
            return values[old]
    return default


def allow_anon(env: Mapping[str, str] | None = None) -> bool:
    """Whether the operator opted in to anonymous access. Only exactly ``"1"`` counts."""
    return setting("CHAT_APP_ALLOW_ANON", env=env) == "1"


def upload_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where the chat app saves uploads; the planner only reads files from inside it."""
    value = setting("CHAT_APP_UPLOAD_DIR", env=env)
    return Path(DEFAULT_UPLOAD_DIR if value is None else value)


def log_old_names() -> None:
    """Log each old-name notice once. Call after logging is configured."""
    global _log_at_once
    _log_at_once = True
    for old in list(_pending):
        message = _pending.pop(old, None)
        if message is not None:
            _logged.add(old)
            logger.warning(message)


def reset() -> None:
    """Forget recorded notices and wait for log_old_names() again (for tests)."""
    global _log_at_once
    _pending.clear()
    _logged.clear()
    _log_at_once = False


def _notice(old: str, message: str) -> None:
    if old in _logged or old in _pending:
        return
    _pending[old] = message
    if _log_at_once:
        log_old_names()
