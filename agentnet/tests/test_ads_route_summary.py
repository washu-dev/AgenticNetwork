from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from agentnet.scripts.show_ads_routes import collect_routes, render_text


def _completed(
    command: list[str],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def test_show_ads_routes_collects_names_labels_and_publication_times(tmp_path: Path) -> None:
    data_dir = tmp_path / "dir"
    data_dir.mkdir()
    _write_publication_db(data_dir / "dir.db", cid="cid-email")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        args = command[3:]
        if args[:2] == ["routing", "list"]:
            return _completed(
                command,
                stdout=json.dumps(
                    [
                        {
                            "record_ref": {"cid": "cid-email"},
                            "labels": ["/skills/email", "/modules/integration/a2a"],
                        }
                    ]
                ),
            )
        if args[:1] == ["pull"]:
            return _completed(
                command,
                stdout=json.dumps(
                    {
                        "annotations": {
                            "agentic_network.agent_id": "email-agent",
                            "agentic_network.display_name": "Email Agent",
                            "a2a.url": "http://email/",
                        },
                        "created_at": "2026-04-30T01:00:00Z",
                        "version": "0.2.5",
                    }
                ),
            )
        raise AssertionError(f"unexpected command: {command}")

    report = collect_routes(server_addr="localhost:8888", data_dir=data_dir, runner=fake_run)

    assert report["routed_count"] == 1
    route = report["routes"][0]
    assert route["name"] == "Email Agent"
    assert route["agent_id"] == "email-agent"
    assert route["endpoint"] == "http://email/"
    assert route["labels"] == ["/skills/email", "/modules/integration/a2a"]
    assert route["label_count"] == 2
    assert route["publish_requested_at"] == "2026-04-30T01:00:01-05:00"
    assert route["routed_at"] == "2026-04-30T01:00:03-05:00"
    assert any(command[3:5] == ["routing", "list"] for command in commands)
    assert any(command[3:5] == ["pull", "cid-email"] for command in commands)


def test_show_ads_routes_render_text_includes_count_and_labels(tmp_path: Path) -> None:
    report = {
        "server_addr": "localhost:8888",
        "routed_count": 1,
        "routes": [
            {
                "cid": "cid-email",
                "agent_id": "email-agent",
                "name": "Email Agent",
                "endpoint": "http://email/",
                "version": "0.2.5",
                "publish_requested_at": "2026-04-30T01:00:01-05:00",
                "routed_at": "2026-04-30T01:00:03-05:00",
                "record_created_at": "2026-04-30T01:00:00Z",
                "labels": ["/skills/email"],
                "label_count": 1,
            }
        ],
    }

    text = render_text(report)

    assert "Routed records: 1" in text
    assert "Email Agent (email-agent)" in text
    assert "routed_at: 2026-04-30T01:00:03-05:00" in text
    assert "labels (1):" in text
    assert "- /skills/email" in text


def _write_publication_db(db_path: Path, *, cid: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            create table publications (
                id text,
                status integer,
                created_time text,
                last_update_time text,
                request_json text
            )
            """
        )
        connection.execute(
            """
            insert into publications (id, status, created_time, last_update_time, request_json)
            values (?, ?, ?, ?, ?)
            """,
            (
                "publication-id",
                3,
                "2026-04-30T01:00:01-05:00",
                "2026-04-30T01:00:03-05:00",
                json.dumps({"recordRefs": {"refs": [{"cid": cid}]}}),
            ),
        )
