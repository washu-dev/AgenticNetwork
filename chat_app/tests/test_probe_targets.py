"""Which agents the chat app's health loop probes (chat_app.server.probe_targets)."""

from __future__ import annotations

from types import SimpleNamespace

from chat_app.server import probe_targets


def _agent(agent_id, endpoint):
    return SimpleNamespace(agent_id=agent_id, endpoint=endpoint)


AGENTS = [
    _agent("local", "http://127.0.0.1:9103/"),
    _agent("cloud", "https://agent-x-abc123-uc.a.run.app/"),
    _agent("skipped", "http://10.0.0.5:19108/"),
    _agent("no-endpoint", ""),
]


def test_cloud_run_and_listed_agents_are_not_probed():
    assert [a.agent_id for a in probe_targets(AGENTS, {"skipped"})] == ["local"]


def test_cloud_run_agents_are_probed_when_asked():
    got = probe_targets(AGENTS, {"skipped"}, probe_run_app=True)
    assert [a.agent_id for a in got] == ["local", "cloud"]
