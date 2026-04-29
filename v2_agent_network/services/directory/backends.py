from __future__ import annotations

import os
from importlib import metadata
from typing import Any, Protocol

from a2a.types import AgentCard

from v2_agent_network.services.common.models import DiscoveredAgent, PublishedRecord
from v2_agent_network.services.directory.card_oasf_converter import (
    agent_card_to_oasf_record,
    oasf_record_to_discovered_agent,
    published_record_from_oasf,
    validate_oasf_record,
)


class DirectoryUnavailable(RuntimeError):
    """Raised when real ADS is requested but unavailable or unconfigured."""


class DirectoryBackend(Protocol):
    def publish_agent_card(self, card: AgentCard) -> PublishedRecord: ...

    def publish_oasf_record(self, record: dict[str, Any]) -> PublishedRecord: ...

    def search_agents(
        self,
        skill: str | None = None,
        domain: str | None = None,
        input_mode: str | None = None,
        output_mode: str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> list[DiscoveredAgent]: ...

    def resolve_agent(self, agent_id_or_record_id: str) -> DiscoveredAgent: ...

    def healthcheck(self) -> bool: ...


class FakeDirectoryBackend:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def publish_agent_card(self, card: AgentCard) -> PublishedRecord:
        return self.publish_oasf_record(agent_card_to_oasf_record(card))

    def publish_oasf_record(self, record: dict[str, Any]) -> PublishedRecord:
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
        return _filter_and_sort_records(
            list(self._records.values()),
            skill=skill,
            domain=domain,
            input_mode=input_mode,
            output_mode=output_mode,
        )

    def resolve_agent(self, agent_id_or_record_id: str) -> DiscoveredAgent:
        for record in self._records.values():
            if agent_id_or_record_id in {record["record_id"], record["agent_id"]}:
                return oasf_record_to_discovered_agent(record)
        raise KeyError(f"Agent not found: {agent_id_or_record_id}")

    def healthcheck(self) -> bool:
        return True


class RealADSDirectoryBackend:
    def __init__(
        self,
        *,
        ads_url: str | None = None,
        namespace: str | None = None,
        org_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.server_address = ads_url or os.getenv("ADS_URL") or os.getenv("ADS_SERVER_ADDRESS")
        self.namespace = namespace or os.getenv("ADS_NAMESPACE")
        self.org_id = org_id or os.getenv("ADS_ORG_ID")
        self.api_key = api_key or os.getenv("ADS_API_KEY")
        self._client: Any | None = None
        self._models: Any | None = None

    def publish_agent_card(self, card: AgentCard) -> PublishedRecord:
        return self.publish_oasf_record(agent_card_to_oasf_record(card))

    def publish_oasf_record(self, record: dict[str, Any]) -> PublishedRecord:
        validate_oasf_record(record)
        client, models = self._client_and_models()
        try:
            from google.protobuf.json_format import ParseDict
            from google.protobuf.struct_pb2 import Struct

            data_struct = Struct()
            ParseDict(record, data_struct)
            ads_record = models.core_v1.Record(data=data_struct)
            refs = client.push([ads_record])
            cid = refs[0].cid if refs else None
            if cid:
                record_refs = models.routing_v1.RecordRefs(
                    refs=[models.core_v1.RecordRef(cid=cid)]
                )
                client.publish(models.routing_v1.PublishRequest(record_refs=record_refs))
            return published_record_from_oasf(record, backend="real", backend_ref=cid)
        except DirectoryUnavailable:
            raise
        except Exception as exc:
            raise self._unavailable("Real ADS publish failed", exc) from exc

    def search_agents(
        self,
        skill: str | None = None,
        domain: str | None = None,
        input_mode: str | None = None,
        output_mode: str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> list[DiscoveredAgent]:
        client, models = self._client_and_models()
        try:
            from google.protobuf.json_format import MessageToDict

            limit = int((constraints or {}).get("limit", 200))
            request = models.search_v1.SearchRecordsRequest(limit=limit)
            responses = client.search_records(request)
            records = [
                MessageToDict(response.record.data)
                for response in responses
                if getattr(response, "record", None) and response.record.data
            ]
        except DirectoryUnavailable:
            raise
        except Exception as exc:
            raise self._unavailable("Real ADS search failed", exc) from exc
        return _filter_and_sort_records(
            _dedupe_records(records),
            skill=skill,
            domain=domain,
            input_mode=input_mode,
            output_mode=output_mode,
        )

    def resolve_agent(self, agent_id_or_record_id: str) -> DiscoveredAgent:
        agents = self.search_agents()
        for agent in agents:
            if agent_id_or_record_id in {agent.agent_id, agent.source_record_id}:
                return agent
        raise KeyError(f"Agent not found: {agent_id_or_record_id}")

    def healthcheck(self) -> bool:
        self._client_and_models()
        return True

    def _client_and_models(self) -> tuple[Any, Any]:
        if not self.server_address:
            raise self._unavailable(
                "Real ADS is not configured. Set ADS_URL or ADS_SERVER_ADDRESS.",
                None,
            )
        if self._client is not None and self._models is not None:
            return self._client, self._models
        try:
            from agntcy.dir_sdk.client import Client, Config
            from agntcy.dir_sdk import models

            self._client = Client(Config(server_address=self.server_address))
            self._models = models
            return self._client, self._models
        except Exception as exc:
            raise self._unavailable("Failed to import or initialize agntcy-dir SDK", exc) from exc

    def _unavailable(self, message: str, error: BaseException | None) -> DirectoryUnavailable:
        original = f"{type(error).__name__}: {error}" if error is not None else "none"
        return DirectoryUnavailable(
            f"{message}. Original error: {original}. "
            f"protobuf={_package_version('protobuf')}; "
            f"agntcy-dir={_package_version('agntcy-dir')}. "
            "Suggested next step: fix AGNTCY/protobuf dependency compatibility "
            "in a separate dependency milestone; do not change dependencies in Milestone 4A."
        )


def _filter_and_sort_records(
    records: list[dict[str, Any]],
    *,
    skill: str | None,
    domain: str | None,
    input_mode: str | None,
    output_mode: str | None,
) -> list[DiscoveredAgent]:
    candidates = [oasf_record_to_discovered_agent(record) for record in records]

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


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record.get("agent_id") or record.get("record_id") or record.get("name")
        if key:
            deduped[str(key)] = record
    return list(deduped.values())


def _package_version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not-installed"
