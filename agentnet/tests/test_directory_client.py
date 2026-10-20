from __future__ import annotations

import pytest

from agentnet.directory.client import DirectoryClient, DirectoryUnavailable


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


@pytest.mark.parametrize(
    ("value", "fake"),
    [("1", True), ("true", True), ("Yes", True), ("y", True), (" on ", True),
     ("false", False), ("0", False), ("", False), ("off", False)],
)
def test_fake_ads_env_accepts_the_chat_app_config_true_values(
    monkeypatch: pytest.MonkeyPatch, value: str, fake: bool
) -> None:
    # Same values as the chat app's config (_bool): a deployment that sets
    # AGENT_NETWORK_FAKE_ADS=1 gets the fake directory everywhere, not just there.
    monkeypatch.setenv("AGENT_NETWORK_FAKE_ADS", value)
    assert DirectoryClient().fake_ads is fake
