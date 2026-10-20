"""Auth endpoints: Google OAuth for per-user credential storage."""

from __future__ import annotations

import logging
import os
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from chat_app.auth import get_current_user
from chat_app.store.crypto import encrypt
from chat_app.store.db import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _google_auth_url() -> str:
    client_id = os.environ["GOOGLE_CLIENT_ID"]
    redirect_uri = os.environ["GOOGLE_REDIRECT_URI"]
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email https://www.googleapis.com/auth/gmail.modify "
                 "https://www.googleapis.com/auth/calendar "
                 "https://www.googleapis.com/auth/drive.file",
        "access_type": "offline",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


@router.get("/logout")
async def logout() -> RedirectResponse:
    """Clear the Shibboleth session and redirect back to the app (which will re-trigger SSO login)."""
    return RedirectResponse("/Shibboleth.sso/Logout?return=/", status_code=303)


@router.get("/google")
async def google_oauth_start(
    _user: Annotated[dict, Depends(get_current_user)],
) -> RedirectResponse:
    return RedirectResponse(_google_auth_url())


@router.get("/google/callback")
async def google_oauth_callback(
    code: str,
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
) -> RedirectResponse:
    client_id = os.environ["GOOGLE_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_CLIENT_SECRET"]
    redirect_uri = os.environ["GOOGLE_REDIRECT_URI"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if resp.status_code != 200:
        logger.error("google_token_exchange_failed status=%d body=%s", resp.status_code, resp.text[:500])
        raise HTTPException(status_code=502, detail=f"Google token exchange failed: {resp.text[:300]}")

    tokens = resp.json()
    payload = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_in": tokens.get("expires_in"),
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    encrypted = encrypt(payload)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_credentials (user_id, provider, encrypted)
            VALUES ($1, 'google_oauth', $2)
            ON CONFLICT (user_id, provider) DO UPDATE SET encrypted = $2, updated_at = now()
            """,
            user["id"],
            encrypted,
        )

    return RedirectResponse(url="/", status_code=303)
