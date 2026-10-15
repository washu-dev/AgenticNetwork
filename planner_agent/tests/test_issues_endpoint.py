from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from planner_agent.main_agent import github_issues
from planner_agent.main_agent.config import MainAgentConfig
from planner_agent.main_agent.directory.card_oasf_converter import load_agent_card
from planner_agent.main_agent.directory.client import DirectoryClient
from planner_agent.main_agent.llm_client import ScriptedLLMClient
from planner_agent.main_agent.server import IssueRequest, build_issue_payload, create_app
from planner_agent.tests.test_main_agent_runtime import FailA2A, EMAIL_CARD


def _config() -> MainAgentConfig:
    return MainAgentConfig(llm_enabled=False, llm_model="mock", fake_ads=True)


def _app(directory: DirectoryClient | None = None):
    return create_app(
        config=_config(),
        directory_client=directory or DirectoryClient(fake_ads=True),
        a2a_client=FailA2A(),
        llm_client=ScriptedLLMClient([]),
    )


# ── build_issue_payload (pure) ───────────────────────────────────────────────

def test_build_payload_bug_labels_and_context() -> None:
    req = IssueRequest(kind="bug", summary="Chat hangs", description="Spun forever",
                       context_id="ctx-1")
    title, body, labels = build_issue_payload(
        req, reporter_email="a@b.edu", timestamp="2026-01-01T00:00:00Z",
        trace_rows=[{"timestamp": "t", "prompt": "hi", "routed_to": "email",
                     "latency_ms": 5, "state": "done"}],
        agent_versions=["email_agent"],
    )
    assert title == "Chat hangs"
    assert labels == ["user-submitted", "bug-report"]
    assert "a@b.edu" in body
    assert "ctx-1" in body
    assert "email_agent" in body
    assert "| email | 5 | done |" in body   # trace row rendered


def test_build_payload_feedback_labels_and_step() -> None:
    req = IssueRequest(kind="agent_creation_feedback", summary="",
                       description="Skill picker confusing", step="validate",
                       agent_id="draft-9")
    title, body, labels = build_issue_payload(
        req, reporter_email="x@y.edu", timestamp="T")
    # Empty summary falls back to (a slice of) the description.
    assert title == "Skill picker confusing"
    assert labels == ["user-submitted", "agent-creation-feedback"]
    assert "Step: validate" in body
    assert "draft-9" in body


def test_build_payload_truncates_long_trace_prompt() -> None:
    req = IssueRequest(kind="bug", summary="s", description="d", context_id="c")
    _, body, _ = build_issue_payload(
        req, reporter_email="a@b.edu", timestamp="T",
        trace_rows=[{"timestamp": "t", "prompt": "P" * 500, "routed_to": "x",
                     "latency_ms": 1, "state": "done"}],
    )
    assert "…(truncated)" in body


# ── POST /issues ─────────────────────────────────────────────────────────────

def test_issues_success(monkeypatch) -> None:
    asyncio.run(_issues_success(monkeypatch))


async def _issues_success(monkeypatch) -> None:
    captured: dict = {}

    async def fake_create(title, body, labels):
        captured.update(title=title, body=body, labels=labels)
        return "https://github.com/example-org/private-reports/issues/42"

    monkeypatch.setattr(github_issues, "create_issue", fake_create)

    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    app = _app(directory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/issues", json={
            "kind": "bug", "summary": "Broken", "description": "It broke",
        })
    assert resp.status_code == 200
    assert resp.json() == {"ok": True,
                           "issue_url": "https://github.com/example-org/private-reports/issues/42"}
    assert captured["labels"] == ["user-submitted", "bug-report"]
    # The endpoint snapshots connected agent names into the body.
    assert "Email Agent" in captured["body"] or "email" in captured["body"].lower()


def test_issues_feedback_kind_passes_through(monkeypatch) -> None:
    asyncio.run(_issues_feedback(monkeypatch))


async def _issues_feedback(monkeypatch) -> None:
    captured: dict = {}

    async def fake_create(title, body, labels):
        captured.update(labels=labels)
        return "url"

    monkeypatch.setattr(github_issues, "create_issue", fake_create)
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/issues", json={
            "kind": "agent_creation_feedback", "summary": "s",
            "description": "d", "step": "submit", "agent_id": "a1",
        })
    assert resp.status_code == 200
    assert captured["labels"] == ["user-submitted", "agent-creation-feedback"]


def test_issues_invalid_kind_422(monkeypatch) -> None:
    asyncio.run(_issues_invalid_kind())


async def _issues_invalid_kind() -> None:
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/issues", json={
            "kind": "nonsense", "summary": "s", "description": "d",
        })
    assert resp.status_code == 422


def test_issues_github_failure_502(monkeypatch) -> None:
    asyncio.run(_issues_github_failure(monkeypatch))


async def _issues_github_failure(monkeypatch) -> None:
    async def boom(title, body, labels):
        raise github_issues.GitHubIssueError("no token")

    monkeypatch.setattr(github_issues, "create_issue", boom)
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/issues", json={
            "kind": "bug", "summary": "s", "description": "d",
        })
    assert resp.status_code == 502
    assert "report" in resp.json()["detail"].lower()


# ── github_issues.create_issue guard ─────────────────────────────────────────

def test_create_issue_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_ISSUES_TOKEN", raising=False)
    with pytest.raises(github_issues.GitHubIssueError):
        asyncio.run(github_issues.create_issue("t", "b", ["l"]))


def _fake_github(monkeypatch) -> list[httpx.Request]:
    """Point create_issue's HTTP client at a recorder instead of api.github.com."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"html_url": "https://github.com/example-org/private-reports/issues/1"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(github_issues, "httpx", SimpleNamespace(
        AsyncClient=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
        HTTPError=httpx.HTTPError,
    ))
    return calls


def test_create_issue_requires_repo(monkeypatch) -> None:
    # No default target: issues carry the reporter's email, prompts and trace.
    calls = _fake_github(monkeypatch)
    monkeypatch.setenv("GITHUB_ISSUES_TOKEN", "tok")
    monkeypatch.delenv("GITHUB_ISSUES_REPO", raising=False)
    with pytest.raises(github_issues.GitHubIssueError, match="GITHUB_ISSUES_REPO"):
        asyncio.run(github_issues.create_issue("t", "b", ["l"]))
    assert calls == []


def test_create_issue_posts_to_configured_repo(monkeypatch) -> None:
    calls = _fake_github(monkeypatch)
    monkeypatch.setenv("GITHUB_ISSUES_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_ISSUES_REPO", "example-org/private-reports")
    url = asyncio.run(github_issues.create_issue("t", "b", ["l"]))
    assert [str(c.url) for c in calls] == ["https://api.github.com/repos/example-org/private-reports/issues"]
    assert url == "https://github.com/example-org/private-reports/issues/1"


def test_issues_without_repo_502_and_no_github_call(monkeypatch) -> None:
    asyncio.run(_issues_without_repo(monkeypatch))


async def _issues_without_repo(monkeypatch) -> None:
    calls = _fake_github(monkeypatch)
    monkeypatch.setenv("GITHUB_ISSUES_TOKEN", "tok")
    monkeypatch.delenv("GITHUB_ISSUES_REPO", raising=False)
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/issues", json={
            "kind": "bug", "summary": "s", "description": "d",
        })
    assert resp.status_code == 502
    assert calls == []
