from __future__ import annotations

import pytest

from agent_directory_service.services.directory.client import DirectoryClient, DirectoryUnavailable


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
