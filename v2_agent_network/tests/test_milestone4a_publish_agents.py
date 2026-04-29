from __future__ import annotations

from pathlib import Path

from v2_agent_network.scripts import publish_agents


def test_publish_agents_dry_run_validates_all_six_cards_without_directory(
    monkeypatch,
    capsys,
) -> None:
    def fail_if_instantiated(*args, **kwargs):
        raise AssertionError("dry-run must not instantiate DirectoryClient")

    monkeypatch.setattr(publish_agents, "DirectoryClient", fail_if_instantiated)

    exit_code = publish_agents.main(["--dry-run"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.count("dry-run") == 6
    assert "weather-agent" in output
    assert "calendar-agent" in output
    assert "email-agent" in output
    assert "photo-geospatial-agent" in output
    assert "intelligence-agent" in output
    assert "coding-agent" in output
    assert "weather/forecast" in output
    assert "http://127.0.0.1:9101/" in output
    assert "backend" in output


def test_publish_agents_publishes_all_six_cards_to_fake_ads(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AGENT_NETWORK_FAKE_ADS", "true")

    exit_code = publish_agents.main([])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.count(" fake") == 6
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
            str(Path("v2_agent_network/configs/agent_cards/weather_agent.card.json")),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "weather-agent" in output
    assert "calendar-agent" not in output


def test_publish_agents_real_ads_unavailable_exits_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.delenv("AGENT_NETWORK_FAKE_ADS", raising=False)
    monkeypatch.setenv("ADS_URL", "localhost:8888")
    monkeypatch.setenv("ADS_BACKEND", "sdk")

    exit_code = publish_agents.main(
        ["--card", "v2_agent_network/configs/agent_cards/weather_agent.card.json"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "DirectoryUnavailable" not in captured.err
    assert "Original error:" in captured.err
    assert "protobuf=" in captured.err
    assert "agntcy-dir=" in captured.err
    assert "protobuf runtime compatible" in captured.err
