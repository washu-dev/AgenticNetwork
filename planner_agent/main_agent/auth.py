"""Shibboleth-based auth — identity comes from Apache-injected headers."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import HTTPException, Request, status

from planner_agent.main_agent.db import get_pool

logger = logging.getLogger(__name__)


def admin_emails() -> set[str]:
    """The set of admin emails from ADMIN_EMAILS (comma-separated, lowercased).

    P4f: admin is derived from the SSO identity, not a shared token. Fail closed —
    an empty/unset ADMIN_EMAILS means there are no admins.
    """
    raw = os.getenv("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_admin(email: str | None) -> bool:
    """Whether an authenticated email is an allowlisted admin."""
    return bool(email) and email.strip().lower() in admin_emails()

# Shibboleth injects these via ShibUseHeaders On. Try in priority order.
# With WashU's attribute release, "eppn" is eduPersonPrincipalName (wustlkey@wustl.edu).
_SHIB_HEADER_PRIORITY = ("mail", "eppn", "remote-user")


def get_shib_email(request: Request) -> str:
    """Extract the authenticated user's email from Shibboleth-injected headers."""
    for header in _SHIB_HEADER_PRIORITY:
        value = request.headers.get(header, "").strip()
        if value:
            return value.lower()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No Shibboleth identity header present — is the request coming through Apache?",
    )


async def get_or_create_user(email: str) -> dict[str, Any]:
    """Return the user row for this email, auto-provisioning on first login."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, email FROM users WHERE email = $1", email)
        if row is None:
            row = await conn.fetchrow(
                "INSERT INTO users (email) VALUES ($1) RETURNING id, email",
                email,
            )
    return {"id": row["id"], "email": row["email"]}


async def get_current_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency — returns the user dict or raises 401."""
    email = get_shib_email(request)
    return await get_or_create_user(email)
