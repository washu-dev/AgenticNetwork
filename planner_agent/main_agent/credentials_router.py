"""Per-user credential management endpoints (/me/credentials/*)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from planner_agent.main_agent.auth import get_current_user
from planner_agent.main_agent.crypto import encrypt
from planner_agent.main_agent.db import get_pool

router = APIRouter(prefix="/me/credentials", tags=["credentials"])

# Providers accepted via the API-key path (not OAuth).
_ALLOWED_PROVIDERS = {"openai", "anthropic", "brave", "google_api", "google_maps"}


class CredentialPayload(BaseModel):
    value: str  # the raw API key


# ── routes ─────────────────────────────────────────────────────────────────────

@router.get("")
async def list_credentials(
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Return the list of provider names the user has connected. Values are never exposed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT provider, updated_at FROM user_credentials WHERE user_id = $1 ORDER BY provider",
            user["id"],
        )
    return {
        "providers": [
            {"provider": r["provider"], "updated_at": r["updated_at"].isoformat()}
            for r in rows
        ]
    }


@router.put("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def upsert_credential(
    provider: str,
    body: CredentialPayload,
    user: Annotated[dict, Depends(get_current_user)],
) -> None:
    """Store or update an API key for the given provider."""
    provider = provider.lower().strip()
    if provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown provider {provider!r}. Allowed: {sorted(_ALLOWED_PROVIDERS)}",
        )
    if not body.value.strip():
        raise HTTPException(status_code=400, detail="Credential value must not be empty.")

    encrypted = encrypt({"api_key": body.value.strip()})
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_credentials (user_id, provider, encrypted)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, provider) DO UPDATE SET encrypted = $3, updated_at = now()
            """,
            user["id"],
            provider,
            encrypted,
        )


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    provider: str,
    user: Annotated[dict, Depends(get_current_user)],
) -> None:
    """Remove a stored credential."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM user_credentials WHERE user_id = $1 AND provider = $2",
            user["id"],
            provider.lower().strip(),
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Credential not found.")
