from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Protocol

from a2a.types import AgentCard

from planner_agent.main_agent.models import DiscoveredAgent, PublishedRecord
from planner_agent.main_agent.directory.card_oasf_converter import (
    agent_card_to_ads_record,
    agent_card_to_oasf_record,
    oasf_record_to_discovered_agent,
    published_record_from_oasf,
    validate_oasf_record,
)

logger = logging.getLogger(__name__)


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

    def unpublish_agent(self, agent_id: str) -> None: ...

    def healthcheck(self) -> bool: ...


class FakeDirectoryBackend:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def publish_agent_card(self, card: AgentCard) -> PublishedRecord:
        return self.publish_oasf_record(agent_card_to_oasf_record(card))

    def publish_oasf_record(self, record: dict[str, Any]) -> PublishedRecord:
        validate_oasf_record(record)
        self._records[record["record_id"]] = record
        logger.debug(
            "directory_fake_publish record_id=%s agent_id=%s",
            record.get("record_id"),
            record.get("agent_id"),
        )
        return published_record_from_oasf(record, backend="fake")

    def search_agents(
        self,
        skill: str | None = None,
        domain: str | None = None,
        input_mode: str | None = None,
        output_mode: str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> list[DiscoveredAgent]:
        agents = _filter_and_sort_records(
            list(self._records.values()),
            skill=skill,
            domain=domain,
            input_mode=input_mode,
            output_mode=output_mode,
        )
        logger.debug(
            "directory_fake_search skill=%s domain=%s results=%d",
            skill,
            domain,
            len(agents),
        )
        return agents

    def resolve_agent(self, agent_id_or_record_id: str) -> DiscoveredAgent:
        for record in self._records.values():
            if agent_id_or_record_id in {record["record_id"], record["agent_id"]}:
                return oasf_record_to_discovered_agent(record)
        raise KeyError(f"Agent not found: {agent_id_or_record_id}")

    def unpublish_agent(self, agent_id: str) -> None:
        to_delete = [rid for rid, rec in self._records.items() if rec.get("agent_id") == agent_id]
        for rid in to_delete:
            del self._records[rid]

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
        return self.publish_oasf_record(agent_card_to_oasf_record(card))

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
        internal_record = self._normalize_for_internal(record)
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as fh:
            json.dump(ads_record, fh, indent=2, sort_keys=True)
            temp_path = Path(fh.name)
        try:
            push = self._run_dirctl(["push", str(temp_path), "--output", "raw"])
            cid = push.stdout.strip()
            if not cid:
                raise self._command_unavailable("dirctl push did not return a CID", push)
            self._run_dirctl(["pull", cid, "--output", "json"])
            publication = self._run_dirctl(["routing", "publish", cid, "--output", "json"])
            self._wait_for_routing_index(cid, publication)
            logger.info(
                "directory_real_publish_cli agent_id=%s cid=%s server=%s",
                internal_record.get("agent_id"),
                cid,
                self.server_address,
            )
            return published_record_from_oasf(internal_record, backend="real", backend_ref=cid)
        finally:
            temp_path.unlink(missing_ok=True)

    def _publish_oasf_record_sdk(self, record: dict[str, Any]) -> PublishedRecord:
        client, models = self._client_and_models()
        ads_record_data = self._normalize_for_ads(record)
        internal_record = self._normalize_for_internal(record)
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
            return published_record_from_oasf(internal_record, backend="real", backend_ref=cid)
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

    def unpublish_agent(self, agent_id: str) -> None:
        """Remove an agent from the ADS routing index by agent_id."""
        result = self._run_dirctl(["routing", "search", "--limit", "200", "--output", "json"])
        cids = _cids_from_routing_output(result.stdout)
        if not cids:
            result = self._run_dirctl(["routing", "list", "--output", "json"])
            cids = _cids_from_routing_output(result.stdout)
        logger.info("directory_unpublish_agent_start agent_id=%s cids_found=%d", agent_id, len(cids))
        for cid in cids:
            try:
                record = self._pull_record(cid)
                if record.get("agent_id") == agent_id:
                    self._run_dirctl(["routing", "unpublish", cid, "--output", "json"])
                    logger.info("directory_unpublish_agent agent_id=%s cid=%s", agent_id, cid)
            except Exception:
                logger.warning("directory_unpublish_agent_cid_error agent_id=%s cid=%s", agent_id, cid, exc_info=True)

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
        logger.info(
            "directory_real_search_start server=%s skill=%s searched_skills=%s domain=%s input_mode=%s output_mode=%s",
            self.server_address,
            skill,
            searched_skills,
            domain,
            input_mode,
            output_mode,
        )

        if searched_skills:
            for search_skill in searched_skills:
                result = self._run_dirctl(
                    ["routing", "search", "--skill", search_skill, "--limit", str(limit), "--output", "json"]
                )
                routing_search_cids = _cids_from_routing_output(result.stdout)
                cids.extend(routing_search_cids)
                logger.debug(
                    "directory_real_search_routing_search skill=%s cids=%s",
                    search_skill,
                    routing_search_cids,
                )
                if not cids:
                    local = self._run_dirctl(
                        ["routing", "list", "--skill", search_skill, "--output", "json"]
                    )
                    routing_list_cids = _cids_from_routing_output(local.stdout)
                    cids.extend(routing_list_cids)
                    logger.debug(
                        "directory_real_search_routing_list skill=%s cids=%s",
                        search_skill,
                        routing_list_cids,
                    )
                if not cids and self._allow_local_store_fallback():
                    local_search = self._run_dirctl(
                        ["search", "--skill", search_skill, "--limit", str(limit), "--output", "json"]
                    )
                    local_search_cids = _cids_from_routing_output(local_search.stdout)
                    cids.extend(local_search_cids)
                    logger.debug(
                        "directory_real_search_local_store skill=%s cids=%s",
                        search_skill,
                        local_search_cids,
                    )
            if not cids:
                routed_all = self._run_dirctl(["routing", "search", "--limit", str(limit), "--output", "json"])
                routed_all_cids = _cids_from_routing_output(routed_all.stdout)
                cids.extend(routed_all_cids)
                logger.debug(
                    "directory_real_search_routing_search_all_for_custom_skill skill=%s cids=%s",
                    skill,
                    routed_all_cids,
                )
            if not cids:
                routed_list_all = self._run_dirctl(["routing", "list", "--output", "json"])
                routed_list_all_cids = _cids_from_routing_output(routed_list_all.stdout)
                cids.extend(routed_list_all_cids)
                logger.debug(
                    "directory_real_search_routing_list_all_for_custom_skill skill=%s cids=%s",
                    skill,
                    routed_list_all_cids,
                )
        else:
            result = self._run_dirctl(["routing", "search", "--limit", str(limit), "--output", "json"])
            routing_search_cids = _cids_from_routing_output(result.stdout)
            cids.extend(routing_search_cids)
            logger.debug("directory_real_search_routing_search_all cids=%s", routing_search_cids)
            if not cids:
                local = self._run_dirctl(["routing", "list", "--output", "json"])
                routing_list_cids = _cids_from_routing_output(local.stdout)
                cids.extend(routing_list_cids)
                logger.debug("directory_real_search_routing_list_all cids=%s", routing_list_cids)
            if not cids and self._allow_local_store_fallback():
                local_search = self._run_dirctl(["search", "--limit", str(limit), "--output", "json"])
                local_search_cids = _cids_from_routing_output(local_search.stdout)
                cids.extend(local_search_cids)
                logger.debug("directory_real_search_local_store_all cids=%s", local_search_cids)

        if not cids and not self._allow_local_store_fallback():
            self._raise_if_storage_only_matches(
                skill=skill,
                domain=domain,
                input_mode=input_mode,
                output_mode=output_mode,
                limit=limit,
            )

        records = [self._pull_record(cid) for cid in _dedupe_values(cids)]
        agents = _filter_and_sort_records(
            _dedupe_records(records),
            skill=skill,
            domain=domain,
            input_mode=input_mode,
            output_mode=output_mode,
        )
        if skill and not agents and not self._allow_local_store_fallback():
            self._raise_if_storage_only_matches(
                skill=skill,
                domain=domain,
                input_mode=input_mode,
                output_mode=output_mode,
                limit=limit,
            )
        logger.info(
            "directory_real_search_done skill=%s raw_cids=%s pulled_records=%d agents=%s",
            skill,
            _dedupe_values(cids),
            len(records),
            [agent.agent_id for agent in agents],
        )
        return agents

    @staticmethod
    def _allow_local_store_fallback() -> bool:
        return os.getenv("ADS_ALLOW_LOCAL_STORE_FALLBACK", "false").lower() == "true"

    def _raise_if_storage_only_matches(
        self,
        *,
        skill: str | None,
        domain: str | None,
        input_mode: str | None,
        output_mode: str | None,
        limit: int,
    ) -> None:
        try:
            storage = self._run_dirctl(["search", "--limit", str(limit), "--output", "json"])
            storage_cids = _dedupe_values(_cids_from_routing_output(storage.stdout))
        except DirectoryUnavailable as exc:
            logger.warning(
                "directory_real_search_storage_diagnostic_failed skill=%s error=%s",
                skill,
                exc,
            )
            return
        if not storage_cids:
            return
        records = []
        for cid in storage_cids:
            try:
                records.append(self._pull_record(cid))
            except DirectoryUnavailable as exc:
                logger.warning(
                    "directory_real_search_storage_pull_failed cid=%s skill=%s error=%s",
                    cid,
                    skill,
                    exc,
                )
        matching_agents = _filter_and_sort_records(
            _dedupe_records(records),
            skill=skill,
            domain=domain,
            input_mode=input_mode,
            output_mode=output_mode,
        )
        if not matching_agents:
            return
        agent_ids = [agent.agent_id for agent in matching_agents]
        raise DirectoryUnavailable(
            "Agent record exists in ADS storage but is not routed/discoverable. "
            f"skill={skill!r}; storage_cids={storage_cids!r}; matching_agents={agent_ids!r}. "
            "Live discovery uses the routing index, not plain dirctl search. "
            "Run the ADS diagnostics or repair routing with the planner agent tools."
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
            record = self._normalize_for_internal(_record_from_pull_output(result.stdout))
            logger.debug(
                "directory_real_pull cid=%s agent_id=%s",
                cid,
                record.get("agent_id"),
            )
            return record
        except json.JSONDecodeError as exc:
            raise self._command_unavailable(f"dirctl pull {cid} did not return JSON", result) from exc

    def _normalize_for_ads(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("schema_version") in {"demo-v2", "demo-v3"}:
            card = record["interaction"]["agent_card"]
            return agent_card_to_ads_record(AgentCard.model_validate(card))
        if "interaction" in record and "discovery" in record:
            return record
        raise self._unavailable("Record is missing supported interaction/discovery metadata.", None)

    def _normalize_for_internal(self, record: dict[str, Any]) -> dict[str, Any]:
        if "interaction" in record and "discovery" in record:
            return record
        card_data = _card_data_from_ads_record(record)
        if card_data is not None:
            return agent_card_to_oasf_record(AgentCard.model_validate(card_data))
        raise self._unavailable("ADS record is missing embedded A2A Agent Card data.", None)

    def _wait_for_routing_index(
        self,
        cid: str,
        publication: subprocess.CompletedProcess[str],
    ) -> None:
        timeout = float(os.getenv("ADS_ROUTING_PUBLISH_TIMEOUT_SECONDS", "8"))
        interval = float(os.getenv("ADS_ROUTING_PUBLISH_POLL_SECONDS", "0.25"))
        routed, last_list = self._poll_routing_index(cid, timeout=timeout, interval=interval)
        if routed:
            logger.info(
                "directory_real_publish_routing_verified cid=%s server=%s",
                cid,
                self.server_address,
            )
            return
        if self._restart_daemon_if_stuck():
            logger.warning(
                "directory_real_publish_routing_stuck_restarting_daemon cid=%s server=%s",
                cid,
                self.server_address,
            )
            self._restart_local_daemon_for_routing_repair(cid)
            routed, last_list = self._poll_routing_index(
                cid,
                timeout=timeout,
                interval=interval,
            )
            if routed:
                logger.info(
                    "directory_real_publish_routing_verified_after_restart cid=%s server=%s",
                    cid,
                    self.server_address,
                )
                return
        raise self._routing_not_indexed(cid, publication, last_list)

    def _poll_routing_index(
        self,
        cid: str,
        *,
        timeout: float,
        interval: float,
    ) -> tuple[bool, str]:
        deadline = time.monotonic() + max(timeout, 0)
        last_list = ""
        while True:
            routed = self._run_dirctl(["routing", "list", "--cid", cid, "--output", "json"])
            last_list = routed.stdout.strip()
            if cid in _cids_from_routing_output(routed.stdout):
                return True, last_list
            if time.monotonic() >= deadline:
                break
            time.sleep(max(interval, 0.01))
        return False, last_list

    @staticmethod
    def _restart_daemon_if_stuck() -> bool:
        return os.getenv("ADS_ROUTING_RESTART_DAEMON_IF_STUCK", "false").lower() == "true"

    def _restart_local_daemon_for_routing_repair(self, cid: str) -> None:
        stop = subprocess.run(
            [self.dirctl_path, "daemon", "stop"],
            text=True,
            capture_output=True,
            check=False,
        )
        logger.warning(
            "directory_real_publish_routing_repair_daemon_stop cid=%s returncode=%s stdout=%r stderr=%r",
            cid,
            stop.returncode,
            stop.stdout.strip(),
            stop.stderr.strip(),
        )
        log_path = Path("planner_agent/ads_daemon_repair.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log_file:
            subprocess.Popen(
                [self.dirctl_path, "daemon", "start"],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = subprocess.run(
                [self.dirctl_path, "daemon", "status"],
                text=True,
                capture_output=True,
                check=False,
            )
            if _daemon_status_is_running(status):
                logger.warning(
                    "directory_real_publish_routing_repair_daemon_started cid=%s",
                    cid,
                )
                return
            time.sleep(0.5)
        raise DirectoryUnavailable(
            "ADS routing publish was stuck and ADS_ROUTING_RESTART_DAEMON_IF_STUCK=true, "
            f"but dirctl daemon did not become healthy after restart. cid={cid!r}"
        )

    def _routing_not_indexed(
        self,
        cid: str,
        publication: subprocess.CompletedProcess[str],
        last_list: str,
    ) -> DirectoryUnavailable:
        try:
            info = self._run_dirctl(["routing", "info", "--output", "json"]).stdout.strip()
        except DirectoryUnavailable as exc:
            info = f"<routing info failed: {exc}>"
        pending = _local_publication_status(cid)
        return DirectoryUnavailable(
            "ADS routing publish was accepted but the record did not appear in the routing index. "
            f"cid={cid!r}; publication_stdout={publication.stdout.strip()!r}; "
            f"publication_stderr={publication.stderr.strip()!r}; "
            f"routing_list_cid={last_list!r}; routing_info={info!r}; "
            f"local_publication_status={pending!r}. "
            "The record may be stored but not discoverable. Run the ADS diagnostics/repair script "
            "or restart the ADS daemon deliberately if the dirctl routing reconciler is stuck."
        )

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


def _card_data_from_ads_record(record: dict[str, Any]) -> dict[str, Any] | None:
    for module in record.get("modules", []):
        if not isinstance(module, dict):
            continue
        data = module.get("data")
        if isinstance(data, dict) and isinstance(data.get("card_data"), dict):
            return data["card_data"]
    return None


def _ads_skill_hints(skill: str | None) -> list[str]:
    if not skill:
        return []
    normalized = skill.strip().lower()
    email_terms = {
        "calendar",
        "docs",
        "document",
        "email",
        "gmail",
        "google",
        "group",
        "group email",
        "group notice",
        "notice",
        "sheets",
    }
    code_terms = {
        "bug",
        "code",
        "coding",
        "program",
        "programming",
        "python",
        "repo",
        "repository",
        "software",
        "test",
    }
    if normalized.startswith("email/") or normalized in email_terms:
        return [
            "natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search",
            "natural_language_processing/natural_language_generation/summarization",
            "retrieval_augmented_generation/retrieval_of_information/document_retrieval",
        ]
    if normalized.startswith("code/") or normalized in code_terms:
        return [
            "analytical_skills/coding_skills",
            "analytical_skills/coding_skills/code_optimization",
            "analytical_skills/coding_skills/code_to_docstrings",
            "evaluation_monitoring/test_case_generation",
            "security_privacy/vulnerability_analysis",
        ]
    return []


def _dedupe_values(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _looks_like_cid(value: str) -> bool:
    return value.startswith(("bafy", "bae", "bag", "zd", "Qm"))


def _daemon_status_is_running(status: subprocess.CompletedProcess[str]) -> bool:
    text = (status.stdout + status.stderr).lower()
    return status.returncode == 0 and "daemon is running" in text and "not running" not in text


def _package_version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not-installed"


def _local_publication_status(cid: str) -> dict[str, Any] | None:
    db_path = Path(os.getenv("ADS_LOCAL_DB_PATH", "~/.agntcy/dir/dir.db")).expanduser()
    if not db_path.exists():
        return None
    try:
        import sqlite3

        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            row = connection.execute(
                """
                select id, status, created_time, last_update_time, request_json
                from publications
                where request_json like ?
                order by created_at desc
                limit 1
                """,
                (f"%{cid}%",),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "status": row[1],
            "created_time": row[2],
            "last_update_time": row[3],
            "request_json": row[4],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "db_path": str(db_path)}
