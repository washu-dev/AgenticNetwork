from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from agent_directory_service.scripts import validate_agent_endpoint


WEATHER_CARD = Path("coding_agent/coding_agent.card.json")


async def _fake_send_external_task(*, card, request, timeout):
    return SimpleNamespace(
        state="completed",
        task_id=request.task_id,
        context_id=request.context_id,
        artifacts=[object()],
        structured_output={"ok": True},
    )


def test_validate_endpoint_without_ads(monkeypatch) -> None:
    def fail_if_instantiated(*args, **kwargs):
        raise AssertionError("validation without --publish must not instantiate DirectoryClient")

    monkeypatch.setattr(validate_agent_endpoint, "DirectoryClient", fail_if_instantiated)
    monkeypatch.setattr(validate_agent_endpoint, "_send_external_task", _fake_send_external_task)

    report = asyncio.run(
        validate_agent_endpoint.validate_endpoint(
            card_path=WEATHER_CARD,
            skill="weather/forecast",
            prompt="What is the weather near Austin?",
            endpoint="http://127.0.0.1:9101/",
        )
    )

    assert report["errors"] == []
    assert report["card_valid"] is True
    assert report["oasf_conversion"] is True
    assert report["endpoint_locator"] is True
    assert report["validation_mode"] == "external"
    assert report["endpoint_reachable"] is True
    assert report["a2a_task_sent"] is True
    assert report["a2a_task_state"] == "completed"
    assert report["task_id"]
    assert report["context_id"]
    assert report["artifacts_count"] == 1
    assert report["structured_output_present"] is True
    assert report["published"] is False


def test_validate_endpoint_publish_fake_ads_when_requested(monkeypatch) -> None:
    monkeypatch.setattr(validate_agent_endpoint, "_send_external_task", _fake_send_external_task)

    report = asyncio.run(
        validate_agent_endpoint.validate_endpoint(
            card_path=WEATHER_CARD,
            skill="weather/forecast",
            prompt="What is the weather near Austin?",
            endpoint="http://127.0.0.1:9101/",
            publish=True,
            fake_ads=True,
        )
    )

    assert report["errors"] == []
    assert report["published"] is True
    assert report["publish_backend"] == "fake"
    assert report["a2a_task_state"] == "completed"


def test_validate_endpoint_publish_real_ads_failure_is_explicit(monkeypatch) -> None:
    monkeypatch.delenv("ADS_URL", raising=False)
    monkeypatch.delenv("ADS_SERVER_ADDR", raising=False)
    monkeypatch.delenv("ADS_SERVER_ADDRESS", raising=False)
    monkeypatch.delenv("ADS_BACKEND", raising=False)

    report = asyncio.run(
        validate_agent_endpoint.validate_endpoint(
            card_path=WEATHER_CARD,
            skill="weather/forecast",
            prompt="What is the weather near Austin?",
            endpoint="http://127.0.0.1:9101/",
            publish=True,
            fake_ads=False,
        )
    )

    assert report["published"] is False
    assert report["a2a_task_sent"] is False
    assert "protobuf=" in report["errors"][0]
    assert "agntcy-dir=" in report["errors"][0]
    assert "ADS_BACKEND=cli" in report["errors"][0]


def test_validate_endpoint_cli_outputs_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(validate_agent_endpoint, "_send_external_task", _fake_send_external_task)

    exit_code = validate_agent_endpoint.main(
        [
            "--card",
            str(WEATHER_CARD),
            "--skill",
            "weather/forecast",
            "--prompt",
            "What is the weather near Austin?",
            "--endpoint",
            "http://127.0.0.1:9101/",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["errors"] == []
    assert output["a2a_task_state"] == "completed"
    assert output["artifacts_count"] == 1


def test_validate_endpoint_rejects_unknown_skill() -> None:
    report = asyncio.run(
        validate_agent_endpoint.validate_endpoint(
            card_path=WEATHER_CARD,
            skill="weather/not_declared",
            prompt="What is the weather near Austin?",
            endpoint="http://127.0.0.1:9101/",
        )
    )

    assert report["a2a_task_sent"] is False
    assert "weather/not_declared" in report["errors"][0]
