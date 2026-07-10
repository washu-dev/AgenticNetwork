from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

from agent_directory_service.scripts import force_route_active_agents
from agent_directory_service.scripts.ads_diagnostics import _classify_cid
from agent_directory_service.scripts.force_route_active_agents import (
    StoredAgentRecord,
    _select_latest_per_agent,
)
from agent_directory_service.services.directory.backends import (
    DirectoryUnavailable,
    RealADSDirectoryBackend,
)
from agent_directory_service.services.directory.card_oasf_converter import (
    agent_card_to_ads_record,
)


class FakeDirctlRunner:
    def __init__(
        self,
        *,
        search_returns: bool = True,
        routing_returns: bool | None = None,
        cid: str = "bafy-email-record",
        routed_skills: set[str] | None = None,
        record: dict[str, Any] | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.pushed_record: dict[str, Any] | None = None
        self.search_returns = search_returns
        self.routing_returns = search_returns if routing_returns is None else routing_returns
        self.cid = cid
        self.routed_skills = routed_skills
        self.record = record

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        args = command[3:]
        if args[:1] == ["push"]:
            with Path(args[1]).open("r", encoding="utf-8") as fh:
                self.pushed_record = json.load(fh)
            return _completed(command, stdout=f"{self.cid}\n")
        if args[:2] == ["routing", "publish"]:
            return _completed(command, stdout="published\n")
        if args[:2] == ["routing", "search"]:
            if not self.routing_returns:
                return _completed(command, stdout="[]")
            if not self._skill_is_routed(args):
                return _completed(command, stdout="[]")
            return _completed(
                command,
                stdout=json.dumps({"results": [{"record_ref": {"cid": self.cid}}]}),
            )
        if args[:2] == ["routing", "list"]:
            if not self.routing_returns:
                return _completed(command, stdout="[]")
            if not self._skill_is_routed(args):
                return _completed(command, stdout="[]")
            return _completed(
                command,
                stdout=json.dumps([{"cid": self.cid}]),
            )
        if args[:1] == ["search"]:
            if not self.search_returns:
                return _completed(command, stdout="[]")
            return _completed(command, stdout=json.dumps([self.cid]))
        if args[:1] == ["pull"]:
            return _completed(command, stdout=json.dumps(self._record()))
        if args[:2] == ["routing", "info"]:
            return _completed(command, stdout="{}")
        raise AssertionError(f"unexpected dirctl command: {command}")

    def _record(self) -> dict[str, Any]:
        if self.record is not None:
            return self.record
        if self.pushed_record is not None:
            return self.pushed_record
        raise AssertionError("FakeDirctlRunner: no record configured")

    def _skill_is_routed(self, args: list[str]) -> bool:
        if self.routed_skills is None or "--cid" in args:
            return True
        if "--skill" not in args:
            return True
        skill = args[args.index("--skill") + 1]
        return skill in self.routed_skills


def test_real_ads_cli_search_custom_skill_falls_back_to_routed_index_post_filter() -> None:
    runner = FakeDirctlRunner(
        cid="bafy-disaster-record",
        routed_skills=set(),
        record=agent_card_to_ads_record(_disaster_card()),
    )
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=runner,
    )

    agents = backend.search_agents(skill="disaster/response_plan")

    assert agents[0].agent_id == "disaster-response-agent"
    assert agents[0].endpoint == "http://AGENT_HOST:9104/"
    assert any(
        command[3:6] == ["routing", "search", "--skill"]
        and command[6] == "disaster/response_plan"
        for command in runner.commands
    )
    assert any(command[3:5] == ["routing", "search"] and "--skill" not in command for command in runner.commands)
    assert any(command[3:4] == ["pull"] for command in runner.commands)


def test_real_ads_cli_missing_dirctl_raises_install_instructions() -> None:
    def missing(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("dirctl")

    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=missing,
    )

    with pytest.raises(DirectoryUnavailable) as exc_info:
        backend.healthcheck()

    assert "dirctl is not installed" in str(exc_info.value)


def test_real_ads_cli_missing_daemon_raises_start_hint() -> None:
    def down(command: list[str]) -> subprocess.CompletedProcess[str]:
        return _completed(command, returncode=1, stderr="connection refused")

    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=down,
    )

    with pytest.raises(DirectoryUnavailable) as exc_info:
        backend.healthcheck()

    assert "dirctl daemon start" in str(exc_info.value)


def test_ads_diagnostics_classifies_routed_queued_stale_and_orphaned_records() -> None:
    publications = {
        "queued-cid": {"status": 1},
        "stale-cid": {"status": 3},
    }

    assert _classify_cid(
        "routed-cid",
        routed_cids={"routed-cid"},
        stored_cids={"routed-cid"},
        publications=publications,
    )["state"] == "healthy"
    assert _classify_cid(
        "queued-cid",
        routed_cids=set(),
        stored_cids={"queued-cid"},
        publications=publications,
    )["state"] == "queued"
    assert _classify_cid(
        "stale-cid",
        routed_cids=set(),
        stored_cids={"stale-cid"},
        publications=publications,
    )["state"] == "stale"
    assert _classify_cid(
        "orphaned-cid",
        routed_cids=set(),
        stored_cids={"orphaned-cid"},
        publications=publications,
    )["state"] == "orphaned"


def test_force_route_active_agents_selects_latest_record_per_agent() -> None:
    older_email = StoredAgentRecord(
        cid="old-email-cid",
        agent_key="email-agent|http://email",
        agent_id="email-agent",
        name="Email Agent",
        endpoint="http://email",
        created_at="2026-04-29T22:00:26Z",
    )
    newer_email = StoredAgentRecord(
        cid="new-email-cid",
        agent_key="email-agent|http://email",
        agent_id="email-agent",
        name="Email Agent",
        endpoint="http://email",
        created_at="2026-04-29T22:01:49Z",
    )
    coding = StoredAgentRecord(
        cid="coding-cid",
        agent_key="coding-agent|http://coding",
        agent_id="coding-agent",
        name="Coding Agent",
        endpoint="http://coding",
        created_at="2026-04-29T22:00:00Z",
    )

    selected, superseded = _select_latest_per_agent([older_email, coding, newer_email])

    assert {record.cid for record in selected} == {"new-email-cid", "coding-cid"}
    assert [record.cid for record in superseded] == ["old-email-cid"]


def test_force_route_active_agents_skips_and_deletes_unpullable_search_records(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        args = command[3:]
        if args[:1] == ["search"]:
            return _completed(command, stdout=json.dumps(["missing-cid", "old-cid", "new-cid"]))
        if args[:1] == ["pull"] and args[1] == "missing-cid":
            return _completed(command, returncode=1, stderr="record not found")
        if args[:1] == ["pull"] and args[1] in {"old-cid", "new-cid"}:
            cid = args[1]
            return _completed(
                command,
                stdout=json.dumps(
                    {
                        "annotations": {
                            "agentic_network.agent_id": "email-agent",
                            "agentic_network.display_name": "Email Agent",
                            "a2a.url": "http://email-agent/",
                        },
                        "created_at": (
                            "2026-04-29T22:00:00Z"
                            if cid == "old-cid"
                            else "2026-04-29T22:01:00Z"
                        ),
                    }
                ),
            )
        if args[:2] == ["routing", "publish"]:
            return _completed(command, stdout="published")
        if args[:2] == ["routing", "list"]:
            return _completed(command, stdout=json.dumps([{"record_ref": {"cid": "new-cid"}}]))
        if args[:2] == ["routing", "unpublish"]:
            return _completed(command, stdout="unpublished")
        if args[:1] == ["delete"]:
            return _completed(command, stdout=json.dumps(args[1]))
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(force_route_active_agents.subprocess, "run", fake_run)

    exit_code = force_route_active_agents.main(
        ["--server-addr", "localhost:8888", "--delete-superseded"]
    )

    assert exit_code == 0
    assert any(command[3:6] == ["routing", "publish", "new-cid"] for command in commands)
    assert any(command[3:5] == ["delete", "missing-cid"] for command in commands)
    assert any(command[3:5] == ["delete", "old-cid"] for command in commands)


def _completed(
    command: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _disaster_card() -> AgentCard:
    return AgentCard(
        name="Disaster Response Agent",
        description="A2A disaster response planning agent.",
        url="http://AGENT_HOST:9104/",
        provider=AgentProvider(
            organization="CSPL Disaster Response Agent",
            url="https://example.invalid/cspl-disaster-response-agent",
        ),
        version="0.3.0",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        defaultInputModes=["text/plain", "application/json"],
        defaultOutputModes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="disaster/response_plan",
                name="Build Disaster Response Plan",
                description="Create immediate action guidance and response plans.",
                tags=[
                    "disaster/response_plan",
                    "disaster",
                    "emergency",
                    "response",
                    "planning",
                    "read",
                ],
                inputModes=["text/plain", "application/json"],
                outputModes=["text/plain", "application/json"],
            ),
            AgentSkill(
                id="disaster/triage",
                name="Triage Disaster Situation",
                description="Identify likely incident type and first actions.",
                tags=["disaster/triage", "disaster", "triage", "risk", "read"],
                inputModes=["text/plain", "application/json"],
                outputModes=["text/plain", "application/json"],
            ),
        ],
    )
