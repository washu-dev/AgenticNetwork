"""Per-user credential management endpoints (/me/credentials/*)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from chat_app.auth import get_current_user
from chat_app.credential_catalog import is_known_type
from chat_app.store.crypto import encrypt
from chat_app.store.db import get_pool, list_all_grants, revoke_grant

router = APIRouter(prefix="/me/credentials", tags=["credentials"])
grants_router = APIRouter(prefix="/me/grants", tags=["credentials"])


def _is_allowed_provider(provider: str) -> bool:
    """Accept catalog credential types (canonical Option-A namespace) or custom:<slug>.

    The stored ``provider`` column now holds catalog ``type`` strings so that
    declaration → grant → injection all match on the same key (docs/AUTH.md §4, Namespace).
    ``google_oauth`` is written only by the OAuth callback, never via this path.
    """
    return is_known_type(provider)


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
    provider = provider.lower().strip()  # catalog types + custom:<slug> are lowercase
    if not _is_allowed_provider(provider):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown credential type {provider!r}. "
                   "Use a catalog type (e.g. 'openai_api_key') or 'custom:<slug>'.",
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


# ── per-agent grants (P4c) ───────────────────────────────────────────────────────

@grants_router.get("")
async def list_grants_route(
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """List the user's per-agent credential grants (which agent may use which cred type)."""
    return {"grants": await list_all_grants(user["id"])}


@grants_router.delete("/{agent_id}/{cred_type}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_grant_route(
    agent_id: str,
    cred_type: str,
    user: Annotated[dict, Depends(get_current_user)],
) -> None:
    """Revoke a previously granted credential from an agent. Next call re-prompts for consent."""
    removed = await revoke_grant(user["id"], agent_id.strip(), cred_type.strip())
    if not removed:
        raise HTTPException(status_code=404, detail="Grant not found.")
