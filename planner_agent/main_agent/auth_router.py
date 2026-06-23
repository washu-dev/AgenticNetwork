"""Auth endpoints: signup, login, logout, Google OAuth."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import bcrypt

from planner_agent.main_agent.auth import (
    SESSION_COOKIE,
    _clear_cookie_kwargs,
    _cookie_kwargs,
    create_session,
    delete_session,
    get_current_user,
)
from planner_agent.main_agent.crypto import encrypt
from planner_agent.main_agent.db import get_pool

router = APIRouter(prefix="/auth", tags=["auth"])

_LOGIN_HTML_PATH = (
    __file__.replace("auth_router.py", "web/login.html")
)


def _load_login_html() -> str:
    with open(_LOGIN_HTML_PATH, encoding="utf-8") as f:
        return f.read()


# ── helpers ────────────────────────────────────────────────────────────────────

def _require_wustl(email: str) -> None:
    if not email.lower().endswith("@wustl.edu"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only @wustl.edu email addresses are allowed.",
        )


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


# ── routes ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    return HTMLResponse(_load_login_html())


@router.post("/signup")
async def signup(
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    email = email.strip().lower()
    _require_wustl(email)
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval("SELECT id FROM users WHERE email = $1", email)
        if existing:
            raise HTTPException(status_code=409, detail="An account with that email already exists.")
        user_id = await conn.fetchval(
            "INSERT INTO users (email, password_hash) VALUES ($1, $2) RETURNING id",
            email,
            pw_hash,
        )

    token = await create_session(user_id)
    resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(**_cookie_kwargs(token))
    return resp


@router.post("/login")
async def login(
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    email = email.strip().lower()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, password_hash FROM users WHERE email = $1", email
        )

    if row is None or not row["password_hash"] or not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = await create_session(row["id"])
    resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(**_cookie_kwargs(token))
    return resp


@router.post("/logout")
async def logout(
    request: Request,
    _user: Annotated[dict, Depends(get_current_user)],
) -> Response:
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        await delete_session(token)
    resp = RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(**_clear_cookie_kwargs())
    return resp


@router.get("/google")
async def google_oauth_start(
    _user: Annotated[dict, Depends(get_current_user)],
) -> RedirectResponse:
    return RedirectResponse(_google_auth_url())


@router.get("/google/callback")
async def google_oauth_callback(
    code: str,
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

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
