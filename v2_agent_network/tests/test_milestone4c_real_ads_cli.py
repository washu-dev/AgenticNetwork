from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

from v2_agent_network.scripts import force_route_active_agents, publish_agents
from v2_agent_network.scripts.ads_diagnostics import _classify_cid
from v2_agent_network.scripts.force_route_active_agents import (
    StoredAgentRecord,
    _select_latest_per_agent,
)
from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.directory import backends as backend_module
from v2_agent_network.services.directory.backends import (
    DirectoryUnavailable,
    RealADSDirectoryBackend,
)
from v2_agent_network.services.directory.card_oasf_converter import (
    agent_card_to_ads_record,
    load_agent_card,
)
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.main import create_app


EMAIL_CARD = Path("v2_agent_network/configs/agent_cards/email_agent.card.json")
WEATHER_CARD = Path("v2_agent_network/configs/agent_cards/weather_agent.card.json")
CODING_CARD = Path("v2_agent_network/configs/agent_cards/coding_agent.card.json")


class FakeDirctlRunner:
    def __init__(
        self,
        *,
        search_returns: bool = True,
        routing_returns: bool | None = None,
        card_path: Path = EMAIL_CARD,
        cid: str = "bafy-email-record",
        routed_skills: set[str] | None = None,
        record: dict[str, Any] | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.pushed_record: dict[str, Any] | None = None
        self.search_returns = search_returns
        self.routing_returns = search_returns if routing_returns is None else routing_returns
        self.card_path = card_path
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
        return agent_card_to_ads_record(load_agent_card(self.card_path))

    def _skill_is_routed(self, args: list[str]) -> bool:
        if self.routed_skills is None or "--cid" in args:
            return True
        if "--skill" not in args:
            return True
        skill = args[args.index("--skill") + 1]
        return skill in self.routed_skills


class FailingA2AClient(A2AClient):
    async def send_task(self, agent, request):
        raise AssertionError(f"{request.skill} should not invoke A2A")


def test_real_ads_cli_publish_pushes_and_publishes_email_record() -> None:
    runner = FakeDirctlRunner()
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=runner,
    )

    published = backend.publish_agent_card(load_agent_card(EMAIL_CARD))

    assert published.backend == "real"
    assert published.backend_ref == "bafy-email-record"
    assert published.raw_record["record_id"]
    assert "email/current_time" in published.raw_record["discovery"]["custom_skill_tags"]
    assert runner.pushed_record is not None
    assert runner.pushed_record["schema_version"] == "1.0.0"
    assert runner.pushed_record["skills"]
    assert "record_id" not in runner.pushed_record
    assert runner.pushed_record["modules"][0]["data"]["card_schema_version"]
    assert "endpoint" not in runner.pushed_record["modules"][0]["data"]
    assert any(command[3:4] == ["push"] for command in runner.commands)
    assert any(command[3:4] == ["pull"] for command in runner.commands)
    assert any(command[3:5] == ["routing", "publish"] for command in runner.commands)
    assert any(command[3:7] == ["routing", "list", "--cid", "bafy-email-record"] for command in runner.commands)


def test_real_ads_cli_publish_fails_when_routing_publish_is_not_indexed(monkeypatch) -> None:
    monkeypatch.setenv("ADS_ROUTING_PUBLISH_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("ADS_ROUTING_RESTART_DAEMON_IF_STUCK", "false")
    runner = FakeDirctlRunner(routing_returns=False)
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=runner,
    )

    with pytest.raises(DirectoryUnavailable) as exc_info:
        backend.publish_agent_card(load_agent_card(EMAIL_CARD))

    error = str(exc_info.value)
    assert "did not appear in the routing index" in error
    assert "bafy-email-record" in error
    assert "routing_info" in error
    assert "local_publication_status" in error


def test_real_ads_cli_publish_can_repair_stuck_routing_with_explicit_restart(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ADS_ROUTING_PUBLISH_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("ADS_ROUTING_RESTART_DAEMON_IF_STUCK", "true")
    runner = FakeDirctlRunner(routing_returns=False)

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["dirctl", "daemon", "stop"]:
            runner.routing_returns = True
            return _completed(command, stdout="Daemon stopped\n")
        if command[:3] == ["dirctl", "daemon", "status"]:
            return _completed(command, stdout="Daemon is running\n")
        raise AssertionError(f"unexpected subprocess.run command: {command}")

    class FakePopen:
        pid = 12345

        def __init__(self, command: list[str], **kwargs) -> None:
            assert command == ["dirctl", "daemon", "start"]

    monkeypatch.setattr(backend_module.subprocess, "run", fake_run)
    monkeypatch.setattr(backend_module.subprocess, "Popen", FakePopen)
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=runner,
    )

    published = backend.publish_agent_card(load_agent_card(EMAIL_CARD))

    assert published.backend_ref == "bafy-email-record"
    assert any(command[3:5] == ["routing", "publish"] for command in runner.commands)


def test_real_ads_cli_search_by_custom_skill_pulls_and_post_filters_record() -> None:
    runner = FakeDirctlRunner()
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=runner,
    )
    backend.publish_agent_card(load_agent_card(EMAIL_CARD))

    agents = backend.search_agents(skill="email/current_time")

    assert agents[0].agent_id == "email-agent"
    assert agents[0].endpoint == "http://10.229.117.223:9103/"
    assert any(
        command[3:6] == ["routing", "search", "--skill"]
        and command[6] == "email/current_time"
        for command in runner.commands
    )
    assert any(command[3:4] == ["pull"] for command in runner.commands)


def test_real_ads_cli_search_by_coding_custom_skill_uses_official_routing_hint() -> None:
    runner = FakeDirctlRunner(
        card_path=CODING_CARD,
        cid="bafy-coding-record",
        routed_skills={"analytical_skills/coding_skills"},
    )
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=runner,
    )
    backend.publish_agent_card(load_agent_card(CODING_CARD))

    agents = backend.search_agents(skill="code/repository_inspection")

    assert agents[0].agent_id == "coding-agent"
    assert any(
        command[3:6] == ["routing", "search", "--skill"]
        and command[6] == "code/repository_inspection"
        for command in runner.commands
    )
    assert any(
        command[3:6] == ["routing", "search", "--skill"]
        and command[6] == "analytical_skills/coding_skills"
        for command in runner.commands
    )


def test_real_ads_cli_search_by_broad_code_hint_uses_official_routing_hint() -> None:
    runner = FakeDirctlRunner(
        card_path=CODING_CARD,
        cid="bafy-coding-record",
        routed_skills={"analytical_skills/coding_skills"},
    )
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=runner,
    )
    backend.publish_agent_card(load_agent_card(CODING_CARD))

    agents = backend.search_agents(skill="code")

    assert agents[0].agent_id == "coding-agent"
    assert any(
        command[3:6] == ["routing", "search", "--skill"]
        and command[6] == "analytical_skills/coding_skills"
        for command in runner.commands
    )


def test_real_ads_cli_search_by_broad_email_hint_uses_official_routing_hint() -> None:
    runner = FakeDirctlRunner(
        card_path=EMAIL_CARD,
        cid="bafy-email-record",
        routed_skills={
            "natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search"
        },
    )
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=runner,
    )
    backend.publish_agent_card(load_agent_card(EMAIL_CARD))

    agents = backend.search_agents(skill="email")

    assert agents[0].agent_id == "email-agent"
    assert any(
        command[3:6] == ["routing", "search", "--skill"]
        and command[6]
        == "natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search"
        for command in runner.commands
    )


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
    assert agents[0].endpoint == "http://10.229.117.223:9104/"
    assert any(
        command[3:6] == ["routing", "search", "--skill"]
        and command[6] == "disaster/response_plan"
        for command in runner.commands
    )
    assert any(command[3:5] == ["routing", "search"] and "--skill" not in command for command in runner.commands)
    assert any(command[3:4] == ["pull"] for command in runner.commands)


def test_real_ads_cli_search_reports_storage_only_records_by_default() -> None:
    runner = FakeDirctlRunner(search_returns=True, routing_returns=True)
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=runner,
    )
    backend.publish_agent_card(load_agent_card(EMAIL_CARD))
    runner.routing_returns = False

    with pytest.raises(DirectoryUnavailable) as exc_info:
        backend.search_agents(skill="email/current_time")

    assert "exists in ADS storage but is not routed/discoverable" in str(exc_info.value)
    assert any(command[3:4] == ["search"] for command in runner.commands)


def test_real_ads_cli_search_local_store_fallback_is_explicit(monkeypatch) -> None:
    monkeypatch.setenv("ADS_ALLOW_LOCAL_STORE_FALLBACK", "true")
    runner = FakeDirctlRunner(search_returns=True, routing_returns=True)
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=runner,
    )
    backend.publish_agent_card(load_agent_card(EMAIL_CARD))
    runner.routing_returns = False

    agents = backend.search_agents(skill="email/current_time")

    assert agents[0].agent_id == "email-agent"
    assert any(command[3:4] == ["search"] for command in runner.commands)


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


def test_fake_ads_and_real_ads_records_do_not_mix() -> None:
    fake_directory = DirectoryClient(fake_ads=True)
    fake_directory.publish_agent_card(load_agent_card(WEATHER_CARD))
    real_backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=FakeDirctlRunner(search_returns=False),
    )

    assert real_backend.search_agents(skill="weather/forecast") == []


def test_publish_agents_real_cli_uses_real_backend_without_fake_fallback(monkeypatch, capsys) -> None:
    runner = FakeDirctlRunner()

    def fake_subprocess_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return runner(command)

    monkeypatch.setenv("AGENT_NETWORK_FAKE_ADS", "false")
    monkeypatch.setenv("ADS_BACKEND", "cli")
    monkeypatch.setenv("ADS_SERVER_ADDR", "localhost:8888")
    monkeypatch.setattr(backend_module.subprocess, "run", fake_subprocess_run)

    exit_code = publish_agents.main(["--card", str(EMAIL_CARD)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "bafy-email-record" in output
    assert " real" in output
    assert " fake" not in output


def test_publish_agents_real_cli_fails_if_routing_publish_is_not_indexed(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("ADS_ROUTING_PUBLISH_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("ADS_ROUTING_RESTART_DAEMON_IF_STUCK", "false")
    runner = FakeDirctlRunner(routing_returns=False)

    def fake_subprocess_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return runner(command)

    monkeypatch.setenv("AGENT_NETWORK_FAKE_ADS", "false")
    monkeypatch.setenv("ADS_BACKEND", "cli")
    monkeypatch.setenv("ADS_SERVER_ADDR", "localhost:8888")
    monkeypatch.setattr(backend_module.subprocess, "run", fake_subprocess_run)

    exit_code = publish_agents.main(["--card", str(EMAIL_CARD)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "did not appear in the routing index" in captured.err
    assert "bafy-email-record" in captured.err


def test_requested_skill_email_send_requires_confirmation_before_a2a() -> None:
    asyncio.run(_requested_skill_email_send_requires_confirmation_before_a2a())


async def _requested_skill_email_send_requires_confirmation_before_a2a() -> None:
    directory = DirectoryClient(fake_ads=True)
    directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    pi_app = create_app(
        directory_client=directory,
        a2a_client=FailingA2AClient(),
        publish_weather_card=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "safety-test",
                "channel": "api",
                "text": "Send this email.",
                "requested_skill": "email/send",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["confirmation_required"] is True
    assert payload["pending_confirmation"]["skill"] == "email/send"
    assert payload["task_ids"] == {}


def test_real_ads_requested_skill_email_send_requires_confirmation_before_a2a() -> None:
    asyncio.run(_real_ads_requested_skill_email_send_requires_confirmation_before_a2a())


async def _real_ads_requested_skill_email_send_requires_confirmation_before_a2a() -> None:
    backend = RealADSDirectoryBackend(
        ads_url="localhost:8888",
        backend="cli",
        runner=FakeDirctlRunner(search_returns=True, routing_returns=True),
    )
    backend.publish_agent_card(load_agent_card(EMAIL_CARD))
    directory = DirectoryClient(fake_ads=False, backend=backend)
    pi_app = create_app(
        directory_client=directory,
        a2a_client=FailingA2AClient(),
        publish_weather_card=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://pi-agent",
    ) as pi_http_client:
        response = await pi_http_client.post(
            "/messages",
            json={
                "user_id": "real-ads-safety-test",
                "channel": "api",
                "text": "Send this email.",
                "requested_skill": "email/send",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["confirmation_required"] is True
    assert payload["pending_confirmation"]["skill"] == "email/send"
    assert payload["task_ids"] == {}


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


@pytest.mark.skipif(
    os.getenv("RUN_REAL_ADS_TESTS") != "true",
    reason="Set RUN_REAL_ADS_TESTS=true and start dirctl daemon for real ADS integration.",
)
def test_real_ads_publish_search_resolve_email_card_manual_integration() -> None:
    directory = DirectoryClient(fake_ads=False)
    published = directory.publish_agent_card(load_agent_card(EMAIL_CARD))
    agents = directory.search_agents(skill="email/current_time")
    resolved = directory.resolve_agent(agents[0].source_record_id)

    assert published.backend == "real"
    assert published.backend_ref
    assert agents[0].endpoint == "http://10.229.117.223:9103/"
    assert resolved.agent_id == "email-agent"


@pytest.mark.skipif(
    os.getenv("RUN_REAL_ADS_TESTS") != "true" or not os.getenv("LIVE_EMAIL_AGENT_URL"),
    reason="Set RUN_REAL_ADS_TESTS=true and LIVE_EMAIL_AGENT_URL for live Email Agent integration.",
)
def test_real_ads_pi_current_time_reaches_live_email_agent_manual_integration() -> None:
    from v2_agent_network.scripts.run_real_ads_email_discovery_demo import parse_args, run_demo

    result = asyncio.run(
        run_demo(
            parse_args(
                [
                    "--skill",
                    "email/current_time",
                    "--prompt",
                    "What time is it for the email agent?",
                    "--endpoint",
                    os.environ["LIVE_EMAIL_AGENT_URL"],
                ]
            )
        )
    )

    assert result["task_id"]
    assert result["pi_response"]["confirmation_required"] is False


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
        url="http://10.229.117.223:9104/",
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
