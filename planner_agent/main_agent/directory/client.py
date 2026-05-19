from __future__ import annotations

import os
from typing import Any

from a2a.types import AgentCard

from planner_agent.main_agent.models import DiscoveredAgent, PublishedRecord
from planner_agent.main_agent.directory.backends import (
    DirectoryBackend,
    DirectoryUnavailable,
    FakeDirectoryBackend,
    RealADSDirectoryBackend,
)


class DirectoryClient:
    def __init__(
        self,
        fake_ads: bool | None = None,
        *,
        ads_url: str | None = None,
        namespace: str | None = None,
        org_id: str | None = None,
        api_key: str | None = None,
        backend: DirectoryBackend | None = None,
    ):
        self.fake_ads = (
            os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true"
            if fake_ads is None
            else fake_ads
        )
        self._backend = backend or (
            FakeDirectoryBackend()
            if self.fake_ads
            else RealADSDirectoryBackend(
                ads_url=ads_url,
                namespace=namespace,
                org_id=org_id,
                api_key=api_key,
            )
        )

    def publish_agent_card(self, card: AgentCard) -> PublishedRecord:
        return self._backend.publish_agent_card(card)

    def publish_oasf_record(self, record: dict[str, Any]) -> PublishedRecord:
        return self._backend.publish_oasf_record(record)

    def search_agents(
        self,
        skill: str | None = None,
        domain: str | None = None,
        input_mode: str | None = None,
        output_mode: str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> list[DiscoveredAgent]:
        return self._backend.search_agents(
            skill=skill,
            domain=domain,
            input_mode=input_mode,
            output_mode=output_mode,
            constraints=constraints,
        )

    def resolve_agent(self, agent_id_or_record_id: str) -> DiscoveredAgent:
        return self._backend.resolve_agent(agent_id_or_record_id)

    def healthcheck(self) -> bool:
        return self._backend.healthcheck()
