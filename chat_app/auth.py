"""Shibboleth-based auth — identity comes from Apache-injected headers."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import HTTPException, Request, status

from agentnet.identity import identity_from_headers
from chat_app.store.db import configured, get_pool

logger = logging.getLogger(__name__)

# Users for runs without a database (DATABASE_URL unset: laptop dev stacks and tests),
# keyed by lowercased email. The id is a uuid5 of the email, so a user keeps
# the same id across chat app restarts even though this store is process-local.
_memory_users: dict[str, dict[str, Any]] = {}


def get_shib_email(request: Request) -> str:
    """The authenticated user's email from the Shibboleth-injected headers, or 401.

    Shibboleth injects them via ShibUseHeaders On; ``agentnet.identity`` reads them
    (``mail``, then ``eppn``, then ``remote-user``; stripped and lowercased).
    """
    email = identity_from_headers(request.headers)
    if email is not None:
        return email
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No Shibboleth identity header present — is the request coming through Apache?",
    )


async def get_or_create_user(email: str) -> dict[str, Any]:
    """Return the user row for this email, auto-provisioning on first login.

    Without a database the user is provisioned in the in-memory store instead.
    """
    if not configured():
        key = email.strip().lower()
        if key not in _memory_users:
            _memory_users[key] = {"id": uuid.uuid5(uuid.NAMESPACE_URL, f"mailto:{key}"), "email": key}
        return dict(_memory_users[key])
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
