"""The chat app's short-lived directory listing cache (chat_app/directory_cache.py)."""

from __future__ import annotations

import threading
import time

import pytest

from agentnet.directory.client import DirectoryClient
from agentnet.models import DiscoveredAgent
from chat_app.directory_cache import CachedDirectory


def _agent(agent_id: str) -> DiscoveredAgent:
    return DiscoveredAgent(
        agent_id=agent_id, name=agent_id, organization="test", description="d",
        endpoint=f"http://{agent_id}/", source_record_id=f"cid-{agent_id}",
    )


class _Directory:
    def __init__(self, agents, delay=0.0):
        self.agents = agents
        self.delay = delay
        self.searches = 0
        self.resolves = 0
        self.fail = False

    def search_agents(self, skill=None, domain=None, input_mode=None, output_mode=None, constraints=None):
        self.searches += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("directory down")
        if skill:
            return [a for a in self.agents if a.agent_id == skill]
        return list(self.agents)

    def resolve_agent(self, identifier):
        self.resolves += 1
        raise KeyError(identifier)

    def healthcheck(self):
        return True


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_listing_is_reused_until_it_expires():
    d, clock = _Directory([_agent("a")]), _Clock()
    cache = CachedDirectory(d, ttl_seconds=10, clock=clock)
    assert [a.agent_id for a in cache.search_agents()] == ["a"]
    d.agents = [_agent("a"), _agent("b")]
    clock.now = 9.9
    assert [a.agent_id for a in cache.search_agents()] == ["a"]
    assert d.searches == 1
    clock.now = 10.0
    assert [a.agent_id for a in cache.search_agents()] == ["a", "b"]
    assert d.searches == 2


def test_filtered_searches_are_not_cached():
    d = _Directory([_agent("a"), _agent("b")])
    cache = CachedDirectory(d, ttl_seconds=10)
    assert [a.agent_id for a in cache.search_agents(skill="b")] == ["b"]
    assert [a.agent_id for a in cache.search_agents(skill="b")] == ["b"]
    assert d.searches == 2


def test_concurrent_callers_share_one_fetch():
    d = _Directory([_agent("a")], delay=0.2)
    cache = CachedDirectory(d, ttl_seconds=10)
    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.search_agents())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert d.searches == 1
    assert len(results) == 8


def test_failures_are_not_cached():
    d = _Directory([_agent("a")])
    d.fail = True
    cache = CachedDirectory(d, ttl_seconds=10)
    with pytest.raises(RuntimeError):
        cache.search_agents()
    d.fail = False
    assert [a.agent_id for a in cache.search_agents()] == ["a"]


def test_resolve_uses_the_cached_listing_then_the_directory():
    d = _Directory([_agent("a")])
    cache = CachedDirectory(d, ttl_seconds=10)
    assert cache.resolve_agent("a").agent_id == "a"
    assert d.resolves == 0
    with pytest.raises(KeyError):
        cache.resolve_agent("missing")
    assert d.resolves == 1


def test_other_calls_go_to_the_wrapped_client():
    cache = CachedDirectory(_Directory([]), ttl_seconds=10)
    assert cache.healthcheck() is True


def test_wraps_the_real_client_type():
    cache = CachedDirectory(DirectoryClient(fake_ads=True), ttl_seconds=10)
    assert cache.search_agents() == cache.search_agents()
