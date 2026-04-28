from __future__ import annotations

import os
from typing import Any

from a2a.types import AgentCard

from v2_agent_network.services.common.models import DiscoveredAgent, PublishedRecord
from v2_agent_network.services.directory.card_oasf_converter import (
    agent_card_to_oasf_record,
    oasf_record_to_discovered_agent,
    published_record_from_oasf,
    validate_oasf_record,
)


class DirectoryUnavailable(RuntimeError):
    """Raised when real ADS is requested but unavailable or unimplemented."""


class DirectoryClient:
    def __init__(self, fake_ads: bool | None = None):
        self.fake_ads = (
            os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true"
            if fake_ads is None
            else fake_ads
        )
        self._records: dict[str, dict[str, Any]] = {}

    def _require_fake(self) -> None:
        if not self.fake_ads:
            raise DirectoryUnavailable(
                "Real ADS backend is not implemented in Milestone 1-2. "
                "Set AGENT_NETWORK_FAKE_ADS=true to use the fake test backend."
            )

    def publish_agent_card(self, card: AgentCard) -> PublishedRecord:
        self._require_fake()
        return self.publish_oasf_record(agent_card_to_oasf_record(card))

    def publish_oasf_record(self, record: dict[str, Any]) -> PublishedRecord:
        self._require_fake()
        validate_oasf_record(record)
        self._records[record["record_id"]] = record
        return published_record_from_oasf(record, backend="fake")

    def search_agents(
        self,
        skill: str | None = None,
        domain: str | None = None,
        input_mode: str | None = None,
        output_mode: str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> list[DiscoveredAgent]:
        self._require_fake()
        candidates = [oasf_record_to_discovered_agent(record) for record in self._records.values()]

        def score(agent: DiscoveredAgent) -> int:
            value = 0
            skill_tokens = set()
            official_names = set()
            custom_tags = set()
            for cap in agent.skills:
                skill_tokens.update(cap.skill_tags)
                custom_tags.update(cap.custom_skill_tags)
                official_names.update(item.name for item in cap.official_oasf_skills)
            if skill:
                if skill in skill_tokens:
                    value += 5
                if skill in official_names:
                    value += 5
                if skill in custom_tags:
                    value += 4
            if domain and domain in agent.domains:
                value += 2
            if input_mode and input_mode in agent.input_modes:
                value += 1
            if output_mode and output_mode in agent.output_modes:
                value += 1
            return value

        filtered = []
        for agent in candidates:
            if skill and score(agent) <= 0:
                continue
            if domain and domain not in agent.domains:
                continue
            if input_mode and input_mode not in agent.input_modes:
                continue
            if output_mode and output_mode not in agent.output_modes:
                continue
            filtered.append(agent)

        return sorted(filtered, key=score, reverse=True)

    def resolve_agent(self, agent_id_or_record_id: str) -> DiscoveredAgent:
        self._require_fake()
        for record in self._records.values():
            if agent_id_or_record_id in {record["record_id"], record["agent_id"]}:
                return oasf_record_to_discovered_agent(record)
        raise KeyError(f"Agent not found: {agent_id_or_record_id}")

    def healthcheck(self) -> bool:
        if self.fake_ads:
            return True
        raise DirectoryUnavailable("Real ADS healthcheck is not implemented in Milestone 1-2")
