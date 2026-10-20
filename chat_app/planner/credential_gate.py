"""Per-agent credential grants gate (P4c).

The runtime consults a ``CredentialGate`` to decide whether a user has granted a
specific credential type to a specific agent, and to record new grants when the
user consents. Injected into ``PlannerRuntime`` so it stays testable without a
DB (use ``InMemoryCredentialGate``); production uses ``DbCredentialGate``
(chat_app/store/grants.py).

Grants key on the **DB user UUID** (not the Shib email), which the server passes
to the runtime via request metadata (``auth_user_id``). Until identity is
fail-closed (P4g/F3), a missing UUID means the gate cannot enforce, so the
runtime injects only the declared types — see runtime._scope_credentials.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CredentialGate(Protocol):
    async def list_granted(self, user_id: str, agent_id: str) -> set[str]:
        """Cred types the user has granted to this agent."""
        ...

    async def add_grant(self, user_id: str, agent_id: str, cred_type: str) -> None:
        """Persist a grant of one cred type to one agent for this user."""
        ...


class InMemoryCredentialGate:
    """In-memory gate for tests / no-DB dev. Stores (user_id, agent_id, cred_type)."""

    def __init__(self, grants: set[tuple[str, str, str]] | None = None):
        self._grants: set[tuple[str, str, str]] = set(grants or set())

    async def list_granted(self, user_id: str, agent_id: str) -> set[str]:
        return {ct for (u, a, ct) in self._grants if u == user_id and a == agent_id}

    async def add_grant(self, user_id: str, agent_id: str, cred_type: str) -> None:
        self._grants.add((user_id, agent_id, cred_type))
