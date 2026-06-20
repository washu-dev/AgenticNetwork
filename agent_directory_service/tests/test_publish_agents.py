from __future__ import annotations

from pathlib import Path

from agent_directory_service.scripts import publish_agents


def test_publish_agents_dry_run_validates_cards_without_directory(
    monkeypatch,
    capsys,
) -> None:
    def fail_if_instantiated(*args, **kwargs):
        raise AssertionError("dry-run must not instantiate DirectoryClient")

    monkeypatch.setattr(publish_agents, "DirectoryClient", fail_if_instantiated)

    exit_code = publish_agents.main([
        "--dry-run",
        "--card", "email_agent/email_agent.card.json",
        "--card", "coding_agent/coding_agent.card.json",
    ])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.count("dry-run") == 2
    assert "email-agent" in output
    assert "coding-agent" in output
    assert "backend" in output


def test_publish_agents_publishes_cards_to_fake_ads(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AGENT_NETWORK_FAKE_ADS", "true")

    exit_code = publish_agents.main([
        "--card", "email_agent/email_agent.card.json",
        "--card", "coding_agent/coding_agent.card.json",
    ])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.count(" fake") == 2
    assert "agent_id" in output
    assert "skills" in output
    assert "endpoint" in output
    assert "record_id/cid" in output
    assert "coding-agent" in output


def test_publish_agents_single_card_dry_run(capsys) -> None:
    exit_code = publish_agents.main(
        [
            "--dry-run",
            "--card",
            "coding_agent/coding_agent.card.json",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "coding-agent" in output
    assert "email-agent" not in output


def test_publish_agents_real_ads_unavailable_exits_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.delenv("AGENT_NETWORK_FAKE_ADS", raising=False)
    monkeypatch.setenv("ADS_URL", "localhost:8888")
    monkeypatch.setenv("ADS_BACKEND", "sdk")

    exit_code = publish_agents.main(
        ["--card", "coding_agent/coding_agent.card.json"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "DirectoryUnavailable" not in captured.err
    assert "Original error:" in captured.err
    assert "protobuf=" in captured.err
    assert "agntcy-dir=" in captured.err
    assert "protobuf runtime compatible" in captured.err
