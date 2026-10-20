"""Postgres-backed credential grants gate (P4c), the production ``CredentialGate``.

The planner consults a gate (``chat_app.planner.credential_gate.CredentialGate``) to
decide whether a user has granted a credential type to an agent, and to record new
grants when the user consents. This one stores grants in the ``agent_credential_grants``
table through db.py; the server passes it to ``PlannerRuntime`` when a database is
configured. Grants key on the DB user UUID, not the Shib email.
"""

from __future__ import annotations

import uuid

from chat_app.store import db


class DbCredentialGate:
    """Postgres-backed gate (production). Thin wrapper over db.py helpers."""

    async def list_granted(self, user_id: str, agent_id: str) -> set[str]:
        return await db.list_grants(uuid.UUID(str(user_id)), agent_id)

    async def add_grant(self, user_id: str, agent_id: str, cred_type: str) -> None:
        await db.add_grant(uuid.UUID(str(user_id)), agent_id, cred_type)
