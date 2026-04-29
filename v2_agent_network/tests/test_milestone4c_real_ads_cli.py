from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from v2_agent_network.scripts import publish_agents
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


class FakeDirctlRunner:
    def __init__(self, *, search_returns: bool = True) -> None:
        self.commands: list[list[str]] = []
        self.pushed_record: dict[str, Any] | None = None
        self.search_returns = search_returns

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        args = command[3:]
        if args[:1] == ["push"]:
            with Path(args[1]).open("r", encoding="utf-8") as fh:
                self.pushed_record = json.load(fh)
            return _completed(command, stdout="bafy-email-record\n")
        if args[:2] == ["routing", "publish"]:
            return _completed(command, stdout="published\n")
        if args[:2] == ["routing", "search"]:
            if not self.search_returns:
                return _completed(command, stdout="[]")
            return _completed(
                command,
                stdout=json.dumps({"results": [{"record_ref": {"cid": "bafy-email-record"}}]}),
            )
        if args[:2] == ["routing", "list"]:
            if not self.search_returns:
                return _completed(command, stdout="[]")
            return _completed(
                command,
                stdout=json.dumps([{"cid": "bafy-email-record"}]),
            )
        if args[:1] == ["pull"]:
            return _completed(command, stdout=json.dumps(self._record()))
        if args[:2] == ["routing", "info"]:
            return _completed(command, stdout="{}")
        raise AssertionError(f"unexpected dirctl command: {command}")

    def _record(self) -> dict[str, Any]:
        if self.pushed_record is not None:
            return self.pushed_record
        return agent_card_to_ads_record(load_agent_card(EMAIL_CARD))


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
    assert published.raw_record["schema_version"] == "1.0.0"
    assert published.raw_record["record_id"]
    assert published.raw_record["skills"]
    assert "email/current_time" in published.raw_record["discovery"]["custom_skill_tags"]
    assert any(command[3:4] == ["push"] for command in runner.commands)
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
