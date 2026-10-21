"""DirectoryPublisher, the registration service's publish path, against a fake dirctl.

It must keep publishing exactly what the old ads_utils publisher did: id-only
skill payloads, no hard-coded legacy routing, the legacy record labels, and the
same errors (ValueError for a card with no oasf: tag, DirectoryUnavailable for
any directory failure).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

import agentnet.directory
from agentnet.directory import backends, client, publisher
from agentnet.directory.card_oasf import agent_card_to_ads_record, oasf_skill_tag
from agentnet.directory.publisher import DirectoryPublisher


class FakeDirctl:
    """Stands in for subprocess.run: answers the dirctl commands the publisher sends."""

    def __init__(self, *, cid: str = "bafy-registered", fail: str | None = None) -> None:
        self.cid = cid
        self.fail = fail  # first dirctl argument (e.g. "push") to answer with exit 1
        self.commands: list[list[str]] = []
        self.pushed: dict[str, Any] | None = None

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        args = command[3:]  # after: dirctl --server-addr <addr>
        if self.fail and args[:1] == [self.fail]:
            return _completed(command, returncode=1, stderr="connection refused")
        if args[:1] == ["push"]:
            self.pushed = json.loads(Path(args[1]).read_text(encoding="utf-8"))
            return _completed(command, stdout=f"{self.cid}\n")
        if args[:1] == ["pull"]:
            return _completed(command, stdout=json.dumps(self.pushed))
        if args[:2] == ["routing", "publish"] or args[:2] == ["routing", "unpublish"]:
            return _completed(command, stdout="{}")
        if args[:2] == ["routing", "list"]:
            return _completed(command, stdout=json.dumps([{"record_ref": {"cid": self.cid}}]))
        raise AssertionError(f"unexpected dirctl command: {command}")


@pytest.fixture
def dirctl(monkeypatch: pytest.MonkeyPatch) -> FakeDirctl:
    fake = FakeDirctl()
    monkeypatch.setattr(publisher.subprocess, "run", fake)
    return fake


def test_publish_sends_id_only_skill_payloads_and_legacy_labels(dirctl: FakeDirctl) -> None:
    # Registration tags cards with its short skill keys; sending that name next to
    # the id would disagree with ADS's taxonomy, so only the id goes out.
    card = _card("Weather Bridge Agent", tags=["weather", oasf_skill_tag("nlp/short_key", 10101)])

    record = DirectoryPublisher(server_addr="localhost:8888").publish(
        card, advertise_url="http://10.0.0.5:9300"
    )

    assert dirctl.pushed is not None
    assert dirctl.pushed["skills"] == [{"id": 10101}]
    assert dirctl.pushed["annotations"]["a2a.url"] == "http://10.0.0.5:9300/"
    assert [command[3:5] for command in dirctl.commands] == [
        ["push", dirctl.commands[0][4]],
        ["pull", "bafy-registered"],
        ["routing", "publish"],
        ["routing", "list"],
    ]
    assert record.backend_ref == "bafy-registered"
    assert record.backend == "real"
    assert record.agent_id == "weather-bridge-agent"
    # Legacy labels, exactly as ads_utils wrote them (deployed chat apps read these records).
    expected_id = str(uuid5(NAMESPACE_URL, "agent-directory-service:Weather Bridge Agent:0.1.0"))
    assert record.record_id == expected_id
    assert dirctl.pushed["annotations"]["agentic_network.record_id"] == expected_id
    assert record.raw_record["schema_version"] == "demo-v2"
    assert record.raw_record["metadata"]["source"] == "agent_directory_service"


def test_publish_adds_no_legacy_routing(dirctl: FakeDirctl) -> None:
    # A card named "Email Agent" that also has a KNOWN_OASF_SKILLS skill id: the
    # read path's hard-coded routing would add four skills; the publisher adds none.
    card = _card("Email Agent", tags=[oasf_skill_tag("nlp/short_key", 10101)])
    card.skills.append(
        AgentSkill(
            id="images_computer_vision/image_classification",
            name="Classify",
            description="Classify an image.",
            tags=[],
        )
    )
    legacy_ids = {skill["id"] for skill in agent_card_to_ads_record(card)["skills"]}
    assert legacy_ids == {10101, 203, 10306, 10202, 60103}

    DirectoryPublisher(server_addr="localhost:8888").publish(card)

    assert dirctl.pushed is not None
    assert dirctl.pushed["skills"] == [{"id": 10101}]
    custom_tags = dirctl.pushed["annotations"]["agentic_network.custom_skill_tags"].split(",")
    assert "images_computer_vision/image_classification" in custom_tags


def test_publish_rejects_card_without_oasf_tag(dirctl: FakeDirctl) -> None:
    card = _card("Weather Bridge Agent", tags=["weather", "demo-custom"])

    with pytest.raises(ValueError, match="No OASF routing skills found for agent 'Weather Bridge Agent'"):
        DirectoryPublisher(server_addr="localhost:8888").publish(card)

    assert dirctl.commands == []  # nothing was pushed


def test_publisher_raises_the_shared_directory_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card("Weather Bridge Agent", tags=[oasf_skill_tag("nlp/short_key", 10101)])
    for name in ("ADS_SERVER_ADDR", "ADS_URL", "ADS_SERVER_ADDRESS", "DIRECTORY_CLIENT_SERVER_ADDRESS"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(agentnet.directory.DirectoryUnavailable, match="not configured"):
        DirectoryPublisher().publish(card)

    down = FakeDirctl(fail="push")
    monkeypatch.setattr(publisher.subprocess, "run", down)
    with pytest.raises(agentnet.directory.DirectoryUnavailable, match="dirctl command failed"):
        DirectoryPublisher(server_addr="localhost:8888").publish(card)

    def missing(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("dirctl")

    monkeypatch.setattr(publisher.subprocess, "run", missing)
    with pytest.raises(agentnet.directory.DirectoryUnavailable, match="dirctl not found"):
        DirectoryPublisher(server_addr="localhost:8888").healthcheck(timeout=1.0)


def test_publish_failure_after_the_record_is_stored_names_its_cid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A record the directory stored but did not route in time may be routed
    # later, so the caller gets its CID to unpublish it.
    card = _card("Weather Bridge Agent", tags=[oasf_skill_tag("nlp/short_key", 10101)])
    pub = DirectoryPublisher(server_addr="localhost:8888")
    monkeypatch.setenv("ADS_ROUTING_PUBLISH_TIMEOUT_SECONDS", "0")
    stored = FakeDirctl()

    def not_yet_indexed(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[3:5] == ["routing", "list"]:
            return _completed(command, stdout="[]")
        return stored(command, **kwargs)

    monkeypatch.setattr(publisher.subprocess, "run", not_yet_indexed)
    with pytest.raises(agentnet.directory.DirectoryUnavailable, match="did not appear") as late:
        pub.publish(card)
    assert late.value.cid == "bafy-registered"

    monkeypatch.setattr(publisher.subprocess, "run", FakeDirctl(fail="push"))
    with pytest.raises(agentnet.directory.DirectoryUnavailable) as refused:
        pub.publish(card)
    assert refused.value.cid is None


def test_unpublish_by_cid(dirctl: FakeDirctl) -> None:
    pub = DirectoryPublisher(server_addr="localhost:8888")

    assert pub.unpublish(None) is False
    assert pub.unpublish("bafy-registered") is True
    assert dirctl.commands == [
        ["dirctl", "--server-addr", "localhost:8888", "routing", "unpublish", "bafy-registered"]
    ]


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param(
            "Error: failed to lookup: failed to receive: rpc error: code = NotFound desc = "
            "failed to lookup record: record not found: bafy-gone",
            id="dirctl",
        ),
        pytest.param("Error: record not found: bafy-gone", id="fake-dirctl"),
    ],
)
def test_unpublish_of_a_record_the_directory_does_not_have_counts_as_removed(
    monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    # A directory that was reset, or a card removed earlier, no longer has the
    # record: the card is gone, so the caller may forget its CID.
    monkeypatch.setattr(
        publisher.subprocess, "run",
        lambda command, **kwargs: _completed(command, returncode=1, stderr=stderr),
    )

    assert DirectoryPublisher(server_addr="localhost:8888").unpublish("bafy-gone") is True


def test_unpublish_reports_other_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    pub = DirectoryPublisher(server_addr="localhost:8888")

    monkeypatch.setattr(publisher.subprocess, "run", FakeDirctl(fail="routing"))
    assert pub.unpublish("bafy-registered") is False  # connection refused

    def hung(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(publisher.subprocess, "run", hung)
    assert pub.unpublish("bafy-registered") is False

    def missing(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("dirctl")

    monkeypatch.setattr(publisher.subprocess, "run", missing)
    assert pub.unpublish("bafy-registered") is False


def test_directory_unavailable_is_one_class() -> None:
    # Callers catch it by class: the chat app's runtime and the registration
    # supervisor must see the same class whichever module raised it.
    assert (
        client.DirectoryUnavailable
        is backends.DirectoryUnavailable
        is publisher.DirectoryUnavailable
        is agentnet.directory.DirectoryUnavailable
    )


def _card(name: str, *, tags: list[str]) -> AgentCard:
    return AgentCard(
        name=name,
        description="Registered agent.",
        url="http://AGENT_HOST:9300/",
        provider=AgentProvider(organization="Demo", url="https://example.invalid/agents/demo"),
        version="0.1.0",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(streaming=True, pushNotifications=False),
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
        skills=[
            AgentSkill(
                id="nlp/short_key",
                name="short_key",
                description="Registered skill.",
                tags=tags,
            )
        ],
    )


def _completed(
    command: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=command, returncode=returncode, stdout=stdout, stderr=stderr)
