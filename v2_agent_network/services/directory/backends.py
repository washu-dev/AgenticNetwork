from __future__ import annotations

import json
import os
import subprocess
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Protocol

from a2a.types import AgentCard

from v2_agent_network.services.common.models import DiscoveredAgent, PublishedRecord
from v2_agent_network.services.directory.card_oasf_converter import (
    agent_card_to_ads_record,
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
        backend: str | None = None,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        dirctl_path: str | None = None,
    ) -> None:
        self.server_address = (
            ads_url
            or os.getenv("ADS_SERVER_ADDR")
            or os.getenv("ADS_URL")
            or os.getenv("ADS_SERVER_ADDRESS")
            or os.getenv("DIRECTORY_CLIENT_SERVER_ADDRESS")
        )
        self.backend = (backend or os.getenv("ADS_BACKEND") or "cli").lower()
        self.namespace = namespace or os.getenv("ADS_NAMESPACE")
        self.org_id = org_id or os.getenv("ADS_ORG_ID")
        self.api_key = api_key or os.getenv("ADS_API_KEY")
        self.dirctl_path = dirctl_path or os.getenv("DIRCTL_BIN", "dirctl")
        self._runner = runner or self._default_runner
        self._client: Any | None = None
        self._models: Any | None = None

    def publish_agent_card(self, card: AgentCard) -> PublishedRecord:
        return self.publish_oasf_record(agent_card_to_ads_record(card))

    def publish_oasf_record(self, record: dict[str, Any]) -> PublishedRecord:
        validate_oasf_record(record)
        if self.backend == "cli":
            return self._publish_oasf_record_cli(record)
        if self.backend == "sdk":
            return self._publish_oasf_record_sdk(record)
        raise self._unavailable(
            f"Unsupported ADS_BACKEND={self.backend!r}. Use ADS_BACKEND=cli or ADS_BACKEND=sdk.",
            None,
        )

    def _publish_oasf_record_cli(self, record: dict[str, Any]) -> PublishedRecord:
        self._require_server_address()
        ads_record = self._normalize_for_ads(record)
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as fh:
            json.dump(ads_record, fh, indent=2, sort_keys=True)
            temp_path = Path(fh.name)
        try:
            push = self._run_dirctl(["push", str(temp_path), "--output", "raw"])
            cid = push.stdout.strip()
            if not cid:
                raise self._command_unavailable("dirctl push did not return a CID", push)
            self._run_dirctl(["routing", "publish", cid])
            return published_record_from_oasf(ads_record, backend="real", backend_ref=cid)
        finally:
            temp_path.unlink(missing_ok=True)

    def _publish_oasf_record_sdk(self, record: dict[str, Any]) -> PublishedRecord:
        client, models = self._client_and_models()
        ads_record_data = self._normalize_for_ads(record)
        try:
            from google.protobuf.json_format import ParseDict
            from google.protobuf.struct_pb2 import Struct

            data_struct = Struct()
            ParseDict(ads_record_data, data_struct)
            ads_record = models.core_v1.Record(data=data_struct)
            refs = client.push([ads_record])
            cid = refs[0].cid if refs else None
            if cid:
                record_refs = models.routing_v1.RecordRefs(
                    refs=[models.core_v1.RecordRef(cid=cid)]
                )
                client.publish(models.routing_v1.PublishRequest(record_refs=record_refs))
            return published_record_from_oasf(ads_record_data, backend="real", backend_ref=cid)
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
        if self.backend == "cli":
            return self._search_agents_cli(
                skill=skill,
                domain=domain,
                input_mode=input_mode,
                output_mode=output_mode,
                constraints=constraints,
            )
        if self.backend != "sdk":
            raise self._unavailable(
                f"Unsupported ADS_BACKEND={self.backend!r}. Use ADS_BACKEND=cli or ADS_BACKEND=sdk.",
                None,
            )
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
        if self.backend == "cli":
            return self._resolve_agent_cli(agent_id_or_record_id)
        agents = self.search_agents()
        for agent in agents:
            if agent_id_or_record_id in {agent.agent_id, agent.source_record_id}:
                return agent
        raise KeyError(f"Agent not found: {agent_id_or_record_id}")

    def healthcheck(self) -> bool:
        if self.backend == "cli":
            self._run_dirctl(["routing", "list", "--output", "json"])
            return True
        self._client_and_models()
        return True

    def _search_agents_cli(
        self,
        *,
        skill: str | None,
        domain: str | None,
        input_mode: str | None,
        output_mode: str | None,
        constraints: dict[str, Any] | None,
    ) -> list[DiscoveredAgent]:
        self._require_server_address()
        limit = int((constraints or {}).get("limit", 200))
        cids: list[str] = []
        searched_skills = [skill] if skill else []
        searched_skills.extend(_ads_skill_hints(skill))
        searched_skills = _dedupe_values([item for item in searched_skills if item])

        if searched_skills:
            for search_skill in searched_skills:
                result = self._run_dirctl(
                    ["routing", "search", "--skill", search_skill, "--limit", str(limit), "--output", "json"]
                )
                cids.extend(_cids_from_routing_output(result.stdout))
                if not cids:
                    local = self._run_dirctl(
                        ["routing", "list", "--skill", search_skill, "--output", "json"]
                    )
                    cids.extend(_cids_from_routing_output(local.stdout))
        else:
            result = self._run_dirctl(["routing", "search", "--limit", str(limit), "--output", "json"])
            cids.extend(_cids_from_routing_output(result.stdout))
            if not cids:
                local = self._run_dirctl(["routing", "list", "--output", "json"])
                cids.extend(_cids_from_routing_output(local.stdout))

        records = [self._pull_record(cid) for cid in _dedupe_values(cids)]
        return _filter_and_sort_records(
            _dedupe_records(records),
            skill=skill,
            domain=domain,
            input_mode=input_mode,
            output_mode=output_mode,
        )

    def _resolve_agent_cli(self, agent_id_or_record_id: str) -> DiscoveredAgent:
        if _looks_like_cid(agent_id_or_record_id):
            return oasf_record_to_discovered_agent(self._pull_record(agent_id_or_record_id))
        agents = self.search_agents()
        for agent in agents:
            if agent_id_or_record_id in {agent.agent_id, agent.source_record_id}:
                return agent
        raise KeyError(f"Agent not found: {agent_id_or_record_id}")

    def _pull_record(self, cid: str) -> dict[str, Any]:
        result = self._run_dirctl(["pull", cid, "--output", "json"])
        try:
            return _record_from_pull_output(result.stdout)
        except json.JSONDecodeError as exc:
            raise self._command_unavailable(f"dirctl pull {cid} did not return JSON", result) from exc

    def _normalize_for_ads(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("schema_version") == "demo-v2":
            card = record["interaction"]["agent_card"]
            return agent_card_to_ads_record(AgentCard.model_validate(card))
        if "interaction" in record and "discovery" in record:
            return record
        raise self._unavailable("Record is missing v2 interaction/discovery metadata.", None)

    def _require_server_address(self) -> None:
        if not self.server_address:
            raise self._unavailable(
                "Real ADS is not configured. Set ADS_SERVER_ADDR or ADS_URL.",
                None,
            )

    def _run_dirctl(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self._require_server_address()
        command = [self.dirctl_path, "--server-addr", self.server_address, *args]
        try:
            completed = self._runner(command)
        except FileNotFoundError as exc:
            raise DirectoryUnavailable(
                "dirctl is not installed or not on PATH. Install it with: "
                "brew tap agntcy/dir https://github.com/agntcy/dir && brew install dirctl, "
                "or download the release binary from https://github.com/agntcy/dir/releases. "
                f"Original error: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise self._command_unavailable(f"dirctl command failed: {' '.join(command)}", completed)
        return completed

    @staticmethod
    def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def _command_unavailable(
        self,
        message: str,
        completed: subprocess.CompletedProcess[str],
    ) -> DirectoryUnavailable:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        daemon_hint = ""
        lowered = f"{stdout}\n{stderr}".lower()
        if any(term in lowered for term in ("connection refused", "connect", "unavailable", "deadline")):
            daemon_hint = " Ensure the local ADS daemon is running: dirctl daemon start."
        return DirectoryUnavailable(
            f"{message}.{daemon_hint} stdout={stdout!r}; stderr={stderr!r}"
        )

    def _client_and_models(self) -> tuple[Any, Any]:
        self._require_server_address()
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
            "If ADS_BACKEND=sdk is required, use an isolated ADS environment with "
            "a protobuf runtime compatible with agntcy-dir gencode. ADS_BACKEND=cli "
            "does not import the Python SDK."
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


def _cids_from_routing_output(raw: str) -> list[str]:
    if not raw.strip():
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("items", "records", "results", "record_refs", "recordRefs", "refs"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
    items = data if isinstance(data, list) else [data]
    cids: list[str] = []
    for item in items:
        cid = _cid_from_item(item)
        if cid:
            cids.append(cid)
    return cids


def _cid_from_item(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return None
    for key in ("cid", "id"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    record_ref = item.get("record_ref") or item.get("recordRef") or item.get("ref")
    if isinstance(record_ref, dict):
        value = record_ref.get("cid") or record_ref.get("id")
        return value if isinstance(value, str) else None
    record = item.get("record")
    if isinstance(record, dict):
        value = record.get("cid") or record.get("id")
        return value if isinstance(value, str) else None
    return None


def _record_from_pull_output(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("dirctl pull returned non-object JSON", raw, 0)
    for key in ("record", "data"):
        value = data.get(key)
        if isinstance(value, dict):
            if "data" in value and isinstance(value["data"], dict):
                return value["data"]
            if "interaction" in value and "discovery" in value:
                return value
    return data


def _ads_skill_hints(skill: str | None) -> list[str]:
    if not skill or not skill.startswith("email/"):
        return []
    return [
        "natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search",
        "natural_language_processing/natural_language_generation/summarization",
        "retrieval_augmented_generation/retrieval_of_information/document_retrieval",
    ]


def _dedupe_values(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _looks_like_cid(value: str) -> bool:
    return value.startswith(("bafy", "bae", "bag", "zd", "Qm"))


def _package_version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not-installed"
