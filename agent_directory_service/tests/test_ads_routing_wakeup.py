from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from agent_directory_service.scripts import manage_ads_lan, watch_ads_routing


def _completed(
    command: list[str],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def test_watch_ads_routing_parses_published_event_cid() -> None:
    event = watch_ads_routing._parse_event_line(
        json.dumps(
            {
                "type": "EVENT_TYPE_RECORD_PUBLISHED",
                "recordRef": {"cid": "new-cid"},
            }
        )
    )

    assert event == watch_ads_routing.RoutingEvent(
        event_type=watch_ads_routing.PUBLISHED,
        cid="new-cid",
    )


def test_watch_ads_routing_ignores_malformed_event_line() -> None:
    assert watch_ads_routing._parse_event_line("not json") is None
    assert watch_ads_routing._parse_event_line("[]") is None
    assert watch_ads_routing._parse_event_line(json.dumps({"type": "EVENT_TYPE_RECORD_PUSHED"})) is None


def test_watch_ads_routing_does_nothing_when_cid_is_already_routed(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _completed(command, stdout=json.dumps([{"record_ref": {"cid": "new-cid"}}]))

    monkeypatch.setattr(watch_ads_routing, "_run", fake_run)

    assert watch_ads_routing._repair_published_cid(
        "new-cid",
        server_addr="localhost:8888",
        timeout=0,
        poll=0,
        restart_daemon_if_stuck=False,
        daemon_config=Path("unused.yaml"),
        daemon_log_file=Path("unused.log"),
    )

    assert commands == [
        [
            "dirctl",
            "--server-addr",
            "localhost:8888",
            "routing",
            "list",
            "--cid",
            "new-cid",
            "--output",
            "json",
        ]
    ]


def test_watch_ads_routing_retries_publish_when_cid_is_missing(monkeypatch) -> None:
    commands: list[list[str]] = []
    routed_after_publish = False

    def fake_run(command: list[str], *, allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
        nonlocal routed_after_publish
        commands.append(command)
        args = command[3:]
        if args[:2] == ["routing", "list"]:
            cid = "new-cid" if routed_after_publish else "other-cid"
            return _completed(command, stdout=json.dumps([{"record_ref": {"cid": cid}}]))
        if args[:2] == ["routing", "publish"]:
            routed_after_publish = True
            return _completed(command, stdout="published")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(watch_ads_routing, "_run", fake_run)

    assert watch_ads_routing._repair_published_cid(
        "new-cid",
        server_addr="localhost:8888",
        timeout=0,
        poll=0,
        restart_daemon_if_stuck=False,
        daemon_config=Path("unused.yaml"),
        daemon_log_file=Path("unused.log"),
    )

    assert any(command[3:6] == ["routing", "publish", "new-cid"] for command in commands)


def test_watch_ads_routing_does_not_restart_daemon_without_explicit_flag(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        args = command[3:] if command[:2] == ["dirctl", "--server-addr"] else command[2:]
        if args[:2] == ["routing", "list"]:
            return _completed(command, stdout="[]")
        if args[:2] == ["routing", "publish"]:
            return _completed(command, stdout="published")
        raise AssertionError(f"unexpected command: {command}")

    def fail_restart(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("daemon restart should not be attempted")

    monkeypatch.setattr(watch_ads_routing, "_run", fake_run)
    monkeypatch.setattr(watch_ads_routing, "_restart_daemon", fail_restart)

    assert not watch_ads_routing._repair_published_cid(
        "new-cid",
        server_addr="localhost:8888",
        timeout=0,
        poll=0,
        restart_daemon_if_stuck=False,
        daemon_config=Path("unused.yaml"),
        daemon_log_file=Path("unused.log"),
    )

    assert not any(command[:3] == ["dirctl", "daemon", "stop"] for command in commands)


def test_manage_ads_lan_starts_daemon_with_fast_routing_config(monkeypatch, tmp_path) -> None:
    config = tmp_path / "daemon.yaml"
    config.write_text("server: {}\n", encoding="utf-8")
    log_path = tmp_path / "daemon.log"
    status_calls = 0
    started: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal status_calls
        if command == ["dirctl", "daemon", "status"]:
            status_calls += 1
            if status_calls == 1:
                return _completed(command, stdout="Daemon is not running\n", returncode=1)
            return _completed(command, stdout="Daemon is running\n")
        raise AssertionError(f"unexpected command: {command}")

    class FakePopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            started.append(command)

    monkeypatch.setattr(manage_ads_lan.subprocess, "run", fake_run)
    monkeypatch.setattr(manage_ads_lan.subprocess, "Popen", FakePopen)

    assert manage_ads_lan._ensure_daemon(config, log_path)
    assert started == [["dirctl", "daemon", "start", "--config", str(config)]]
