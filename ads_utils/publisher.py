"""DirectoryPublisher — canonical ADS publish/unpublish logic for all agents.

Drawn from planner_agent/main_agent/directory/backends.py (RealADSDirectoryBackend),
which is the only implementation that verifies the push succeeded and polls until
the routing index actually reflects the new record. All per-agent copies are
fire-and-forget; this one is not.

Usage:
    from ads_utils import DirectoryPublisher, PublishedRecord

    pub = DirectoryPublisher(server_addr="localhost:8888")
    record = pub.publish(card, advertise_url="http://localhost:9103/")
    # ... later ...
    pub.unpublish(record.backend_ref)
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from a2a.types import AgentCard

from ads_utils.converter import agent_card_to_ads_record, agent_card_to_oasf_record

logger = logging.getLogger(__name__)


@dataclass
class PublishedRecord:
    record_id: str
    agent_id: str
    backend_ref: str  # CID returned by dirctl push


class DirectoryUnavailable(RuntimeError):
    pass


class DirectoryPublisher:
    """Publishes and unpublishes an agent card to/from the ADS routing index.

    Args:
        server_addr: ADS server address, e.g. "localhost:8888". Falls back to
            ADS_SERVER_ADDR / ADS_URL env vars if not provided.
        dirctl_path: Path to the dirctl binary. Defaults to DIRCTL_BIN env var
            or "dirctl" (searched on PATH).
    """

    def __init__(
        self,
        server_addr: str | None = None,
        *,
        dirctl_path: str | None = None,
    ) -> None:
        self.server_addr = (
            server_addr
            or os.getenv("ADS_SERVER_ADDR")
            or os.getenv("ADS_URL")
            or os.getenv("ADS_SERVER_ADDRESS")
            or os.getenv("DIRECTORY_CLIENT_SERVER_ADDRESS")
        )
        self.dirctl_path = dirctl_path or os.getenv("DIRCTL_BIN", "dirctl")

    def publish(
        self,
        card: AgentCard,
        advertise_url: str | None = None,
    ) -> PublishedRecord:
        """Convert card to an ADS record, push it, publish to routing index.

        Args:
            card: The A2A AgentCard to publish.
            advertise_url: Override the card's url field before publishing.
                Use this to set the live endpoint (e.g. "http://localhost:9103/").

        Returns:
            PublishedRecord with the CID needed to unpublish later.

        Raises:
            DirectoryUnavailable: If dirctl is missing, the server is
                unreachable, or the routing index does not reflect the record
                within the timeout.
        """
        self._require_server_addr()

        if advertise_url:
            card = card.model_copy(update={"url": advertise_url.rstrip("/") + "/"})

        oasf_record = agent_card_to_oasf_record(card)
        ads_record = agent_card_to_ads_record(card)

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", encoding="utf-8", delete=False
        ) as fh:
            json.dump(ads_record, fh, indent=2, sort_keys=True)
            temp_path = Path(fh.name)

        try:
            push = self._run(["push", str(temp_path), "--output", "raw"])
            cid = push.stdout.strip()
            if not cid:
                raise DirectoryUnavailable("dirctl push did not return a CID")

            self._run(["pull", cid, "--output", "json"])
            self._run(["routing", "publish", cid, "--output", "json"])
            self._wait_for_routing_index(cid)

            logger.info(
                "ads_publish agent_id=%s cid=%s server=%s",
                oasf_record.get("agent_id"),
                cid,
                self.server_addr,
            )
            return PublishedRecord(
                record_id=oasf_record["record_id"],
                agent_id=oasf_record["agent_id"],
                backend_ref=cid,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    def unpublish(self, cid: str | None) -> bool:
        """Remove a record from the ADS routing index.

        Args:
            cid: The CID returned by publish(). No-op if None or empty.

        Returns:
            True if unpublished, False if skipped (no cid).
        """
        if not cid:
            return False
        try:
            self._run(["routing", "unpublish", cid])
            logger.info("ads_unpublish cid=%s server=%s", cid, self.server_addr)
            return True
        except DirectoryUnavailable as exc:
            logger.warning("ads_unpublish_failed cid=%s error=%s", cid, exc)
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _require_server_addr(self) -> None:
        if not self.server_addr:
            raise DirectoryUnavailable(
                "ADS server address is not configured. "
                "Pass server_addr= or set ADS_SERVER_ADDR / ADS_URL."
            )

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self._require_server_addr()
        command = [self.dirctl_path, "--server-addr", self.server_addr, *args]
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False)
        except FileNotFoundError as exc:
            raise DirectoryUnavailable(
                f"dirctl not found at {self.dirctl_path!r}. "
                "Install it from https://github.com/agntcy/dir/releases."
            ) from exc
        if result.returncode != 0:
            output = (result.stderr or result.stdout).strip()
            raise DirectoryUnavailable(
                f"dirctl command failed: {' '.join(command)}\n{output}"
            )
        return result

    def _wait_for_routing_index(self, cid: str) -> None:
        timeout = float(os.getenv("ADS_ROUTING_PUBLISH_TIMEOUT_SECONDS", "8"))
        interval = float(os.getenv("ADS_ROUTING_PUBLISH_POLL_SECONDS", "0.25"))
        deadline = time.monotonic() + max(timeout, 0)

        while True:
            result = self._run(["routing", "list", "--cid", cid, "--output", "json"])
            if cid in result.stdout:
                logger.info("ads_routing_verified cid=%s", cid)
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(max(interval, 0.01))

        raise DirectoryUnavailable(
            f"ADS routing publish accepted but record did not appear in routing index "
            f"within {timeout}s. cid={cid!r}. "
            "The record may be stored but not discoverable — run the ADS repair script."
        )
