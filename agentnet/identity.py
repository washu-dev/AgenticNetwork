"""The signed-in user's identity, as the SSO front door passes it, and the admin list.

In production an Apache/Shibboleth service provider authenticates the user and
passes their identity to the chat app in request headers; the chat app forwards it
to the registration service on its ``/register/*`` pass-through. Both services read
it the same way, here. Admin rights come from that identity: ``ADMIN_EMAILS``
(comma-separated) lists the admins, and an empty or unset list means no admins.

What a request without an identity gets (a 401, the chat app's anonymous stand-in,
a 403) is each service's own decision and stays there.

Standard library only: both service venvs import it, and so do their images. The
dev identity proxy (``devstack/dev_identity_proxy.py``) and agent_skeleton's
executor run where agentnet is not installed, so they keep their own copies of the
header names; tests check that the copies match.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

# The identity headers, in the order they are tried. With WashU's attribute release,
# "eppn" is eduPersonPrincipalName (wustlkey@wustl.edu).
IDENTITY_HEADERS: tuple[str, ...] = ("mail", "eppn", "remote-user")


def normalize_identity(value: str | None) -> str | None:
    """``value`` stripped and lowercased, or None when that leaves nothing."""
    value = (value or "").strip().lower()
    return value or None


def identity_from_headers(headers: Mapping[str, str]) -> str | None:
    """The first non-blank identity header, normalized, or None when there is none.

    ``headers`` is anything with ``.get``: a request's case-insensitive headers, or
    a plain dict with lowercase names. Only the hyphenated spellings count, not
    ``remote_user``.
    """
    for name in IDENTITY_HEADERS:
        identity = normalize_identity(headers.get(name))
        if identity is not None:
            return identity
    return None


def admin_emails(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The identities listed in ``ADMIN_EMAILS``, normalized.

    Empty or unset means no admins (fail closed). ``env`` defaults to ``os.environ``.
    """
    values = os.environ if env is None else env
    listed = (normalize_identity(e) for e in values.get("ADMIN_EMAILS", "").split(","))
    return frozenset(e for e in listed if e is not None)


def is_admin(identity: str | None, env: Mapping[str, str] | None = None) -> bool:
    """Whether ``identity`` (normalized here) is listed in ``ADMIN_EMAILS``."""
    identity = normalize_identity(identity)
    return identity is not None and identity in admin_emails(env)


def without_identity_headers(items: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """The ``(name, value)`` header pairs in ``items`` that are not identity headers.

    Names are compared case-insensitively, and underscore spellings (``remote_user``)
    count too, since some servers map them to the hyphenated name.
    """
    return [
        (name, value) for name, value in items
        if name.strip().lower().replace("_", "-") not in IDENTITY_HEADERS
    ]
