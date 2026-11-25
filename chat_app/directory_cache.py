"""A short-lived cache of the chat app's directory listing.

Every chat turn that lists or resolves agents asks the directory for the full
listing, and with the dirctl CLI backend each listing launches one ``dirctl`` per
agent. On Cloud Run that CPU capped one chat-app instance at about 50 turns a
minute with only two agents in the directory (benchmarks/RESULTS.md §11, in the
workspace above this repo). Caching the full listing for a few seconds turns
those launches into one listing per interval, however many turns arrive.

Only the unfiltered listing is cached (the chat app always asks for it); a call
with filters goes straight to the directory. One listing is fetched at a time:
callers that arrive while it runs wait for it and share its result. Failures are
not cached. The trade-off: a newly registered agent can take up to the cache time
to appear. ``CHAT_APP_DIRECTORY_CACHE_SECONDS`` sets it; 0 (the default) turns
the cache off.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from agentnet.models import DiscoveredAgent


class CachedDirectory:
    """Wraps a directory client; caches ``search_agents()`` for ``ttl_seconds``.

    The chat app calls the directory from worker threads (asyncio.to_thread), so
    the cache uses a threading lock. Everything not overridden here goes to the
    wrapped client.
    """

    def __init__(self, directory: Any, ttl_seconds: float, clock=time.monotonic) -> None:
        self._directory = directory
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._agents: list[DiscoveredAgent] | None = None
        self._fetched_at = 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._directory, name)

    def _fresh(self) -> bool:
        return self._agents is not None and self._clock() - self._fetched_at < self._ttl

    def search_agents(self, *args: Any, **kwargs: Any) -> list[DiscoveredAgent]:
        if args or any(v is not None for v in kwargs.values()):
            return self._directory.search_agents(*args, **kwargs)
        if self._fresh():
            return list(self._agents)
        with self._lock:
            # Another thread may have fetched while this one waited for the lock.
            if not self._fresh():
                self._agents = list(self._directory.search_agents())
                self._fetched_at = self._clock()
            return list(self._agents)

    def resolve_agent(self, agent_id_or_record_id: str) -> DiscoveredAgent:
        """Resolve from the cached listing first; ask the directory on a miss."""
        for agent in self.search_agents():
            if agent_id_or_record_id in {agent.agent_id, agent.source_record_id}:
                return agent
        return self._directory.resolve_agent(agent_id_or_record_id)

    def invalidate(self) -> None:
        with self._lock:
            self._agents = None
