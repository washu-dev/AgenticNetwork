"""Session-based auth dependency and helpers."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import Cookie, HTTPException, status

from planner_agent.main_agent.db import get_pool

SESSION_COOKIE = "planner_session"
SESSION_TTL_HOURS = 24


async def create_session(user_id: UUID) -> str:
    token = secrets.token_hex(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES ($1, $2, $3)",
            token,
            user_id,
            expires_at,
        )
    return token


async def delete_session(token: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM sessions WHERE token = $1", token)


async def get_current_user(
    planner_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict[str, Any]:
    """FastAPI dependency — returns the user row or raises 401."""
    if not planner_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.email
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = $1 AND s.expires_at > now()
            """,
            planner_session,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
        )
    return {"id": row["id"], "email": row["email"]}


def _cookie_kwargs(token: str) -> dict[str, Any]:
    secure = os.getenv("AUTH_COOKIE_SECURE", "true").lower() != "false"
    return dict(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=SESSION_TTL_HOURS * 3600,
        path="/",
    )


def _clear_cookie_kwargs() -> dict[str, Any]:
    return dict(
        key=SESSION_COOKIE,
        value="",
        httponly=True,
        samesite="lax",
        path="/",
        max_age=0,
    )
