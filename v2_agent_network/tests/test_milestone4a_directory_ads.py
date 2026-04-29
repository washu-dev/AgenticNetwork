from __future__ import annotations

import pytest

from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient, DirectoryUnavailable


WEATHER_CARD = "v2_agent_network/configs/agent_cards/weather_agent.card.json"


def test_fake_backend_publish_search_resolve_still_works() -> None:
    directory = DirectoryClient(fake_ads=True)
    published = directory.publish_agent_card(load_agent_card(WEATHER_CARD))

    assert published.backend == "fake"
    assert directory.healthcheck() is True
    assert directory.search_agents(skill="weather/forecast")[0].agent_id == "weather-agent"
    assert directory.resolve_agent(published.record_id).agent_id == "weather-agent"


def test_real_backend_missing_config_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADS_URL", raising=False)
    monkeypatch.delenv("ADS_SERVER_ADDR", raising=False)
    monkeypatch.delenv("ADS_SERVER_ADDRESS", raising=False)
    directory = DirectoryClient(fake_ads=False)

    with pytest.raises(DirectoryUnavailable) as exc_info:
        directory.healthcheck()

    message = str(exc_info.value)
    assert "ADS_SERVER_ADDR or ADS_URL" in message
    assert "protobuf=" in message
    assert "agntcy-dir=" in message
    assert "ADS_BACKEND=cli" in message


def test_real_backend_sdk_import_failure_reports_dependency_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADS_BACKEND", "sdk")
    directory = DirectoryClient(fake_ads=False, ads_url="localhost:8888")

    with pytest.raises(DirectoryUnavailable) as exc_info:
        directory.healthcheck()

    message = str(exc_info.value)
    assert "Original error:" in message
    assert "protobuf=" in message
    assert "agntcy-dir=" in message
    assert "protobuf runtime compatible" in message


def test_real_backend_never_reads_fake_backend_records() -> None:
    fake_directory = DirectoryClient(fake_ads=True)
    fake_directory.publish_agent_card(load_agent_card(WEATHER_CARD))
    real_directory = DirectoryClient(fake_ads=False, ads_url="localhost:8888")

    with pytest.raises(DirectoryUnavailable):
        real_directory.search_agents(skill="weather/forecast")
