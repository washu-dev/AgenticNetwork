"""Per-agent credential grants gate (P4c).

The runtime consults a ``CredentialGate`` to decide whether a user has granted a
specific credential type to a specific agent, and to record new grants when the
user consents. Injected into ``MainAgentRuntime`` so it stays testable without a
DB (use ``InMemoryCredentialGate``); production uses ``DbCredentialGate``.

Grants key on the **DB user UUID** (not the Shib email), which the server passes
to the runtime via request metadata (``auth_user_id``). Until identity is
fail-closed (P4g/F3), a missing UUID means the gate cannot enforce and the
runtime falls back to today's pass-through behavior — see runtime._enforce_credentials.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class CredentialGate(Protocol):
    async def list_granted(self, user_id: str, agent_id: str) -> set[str]:
        """Cred types the user has granted to this agent."""
        ...

    async def add_grant(self, user_id: str, agent_id: str, cred_type: str) -> None:
        """Persist a grant of one cred type to one agent for this user."""
        ...


class DbCredentialGate:
    """Postgres-backed gate (production). Thin wrapper over db.py helpers."""

    async def list_granted(self, user_id: str, agent_id: str) -> set[str]:
        from planner_agent.main_agent import db
        return await db.list_grants(uuid.UUID(str(user_id)), agent_id)

    async def add_grant(self, user_id: str, agent_id: str, cred_type: str) -> None:
        from planner_agent.main_agent import db
        await db.add_grant(uuid.UUID(str(user_id)), agent_id, cred_type)


class InMemoryCredentialGate:
    """In-memory gate for tests / no-DB dev. Stores (user_id, agent_id, cred_type)."""

    def __init__(self, grants: set[tuple[str, str, str]] | None = None):
        self._grants: set[tuple[str, str, str]] = set(grants or set())

    async def list_granted(self, user_id: str, agent_id: str) -> set[str]:
        return {ct for (u, a, ct) in self._grants if u == user_id and a == agent_id}

    async def add_grant(self, user_id: str, agent_id: str, cred_type: str) -> None:
        self._grants.add((user_id, agent_id, cred_type))
