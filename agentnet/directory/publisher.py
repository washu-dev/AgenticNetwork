"""DirectoryPublisher: the registration service's ADS publish/unpublish logic.

Like RealADSDirectoryBackend's publish (backends.py), it verifies the push
succeeded and polls until the routing index actually reflects the new record.
It is deliberately smaller than that backend: no daemon restart, no SDK backend
and no local-store fallback. It reads only the server address (ADS_SERVER_ADDR,
ADS_URL, ADS_SERVER_ADDRESS, DIRECTORY_CLIENT_SERVER_ADDRESS), DIRCTL_BIN,
DIRCTL_TIMEOUT_SECONDS and ADS_ROUTING_PUBLISH_{TIMEOUT,POLL}_SECONDS. ENGR shares
one .env across services, so the backend's other ADS_* settings must not start
affecting the registration service through this class.

Records are built with legacy_routing=False: an agent is routed only by the
'oasf:<name>:<id>' tags on its own card (see card_oasf.oasf_skill_tag).

Usage:
    from agentnet.directory.publisher import DirectoryPublisher

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
from pathlib import Path

from a2a.types import AgentCard

from agentnet.directory import DirectoryUnavailable  # also imported from here
from agentnet.directory.card_oasf import (
    ads_routing_skills,
    agent_card_to_ads_record,
    agent_card_to_oasf_record,
    published_record_from_oasf,
    validate_agent_card,
)
from agentnet.models import PublishedRecord

logger = logging.getLogger(__name__)

# How dirctl reports a CID the directory does not store: dirctl v1.2.0's
# `routing unpublish` looks the record up first and fails with
# "... code = NotFound desc = failed to lookup record: record not found: <cid>";
# the dev stack's fake dirctl prints "record not found: <cid>".
_RECORD_NOT_FOUND = "record not found"


class DirectoryPublisher:
    """Publishes and unpublishes an agent card to/from the ADS routing index.

    Args:
        server_addr: ADS server address, e.g. "localhost:8888". Falls back to
            ADS_SERVER_ADDR / ADS_URL env vars if not provided.
        dirctl_path: Path to the dirctl binary. Defaults to DIRCTL_BIN env var
            or "dirctl" (searched on PATH).

    Each dirctl call is bounded by DIRCTL_TIMEOUT_SECONDS (default 60): a hung
    daemon connection surfaces as DirectoryUnavailable instead of blocking forever.
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
            PublishedRecord whose backend_ref is the CID needed to unpublish later.

        Raises:
            ValueError: If the card is invalid or has no 'oasf:<name>:<id>' tag.
            DirectoryUnavailable: If dirctl is missing, the server is
                unreachable, or the routing index does not reflect the record
                within the timeout. Once the record is stored, the exception's
                ``cid`` names it: it may still be routed later, so the caller
                keeps the CID to unpublish it.
        """
        self._require_server_addr()

        if advertise_url:
            card = card.model_copy(update={"url": advertise_url.rstrip("/") + "/"})

        validate_agent_card(card)
        if not ads_routing_skills(card, legacy_routing=False):
            raise ValueError(
                f"No OASF routing skills found for agent '{card.name}'. Add an "
                "'oasf:<name>:<id>' tag (see oasf_skill_tag) to one of its skills."
            )
        oasf_record = agent_card_to_oasf_record(card, legacy_routing=False)
        ads_record = agent_card_to_ads_record(card, legacy_routing=False)

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

            try:
                self._run(["pull", cid, "--output", "json"])
                self._run(["routing", "publish", cid, "--output", "json"])
                self._wait_for_routing_index(cid)
            except DirectoryUnavailable as exc:
                exc.cid = cid
                raise

            logger.info(
                "ads_publish agent_id=%s cid=%s server=%s",
                oasf_record.get("agent_id"),
                cid,
                self.server_addr,
            )
            return published_record_from_oasf(oasf_record, backend="real", backend_ref=cid)
        finally:
            temp_path.unlink(missing_ok=True)

    def healthcheck(self, timeout: float = 3.0) -> None:
        """Raise DirectoryUnavailable unless the directory answers a cheap read
        within ``timeout`` seconds (the same probe RealADSDirectoryBackend uses)."""
        self._run(["routing", "list", "--output", "json"], timeout=timeout)

    def unpublish(self, cid: str | None) -> bool:
        """Remove a record from the ADS routing index.

        Args:
            cid: The CID returned by publish(). No-op if None or empty.

        Returns:
            True if the record is no longer routed: it was unpublished, or the
            directory does not have it at all (dirctl reports "record not found",
            e.g. after a directory reset or when it was removed earlier). False
            if skipped (no cid) or on any other failure (unreachable, timed out,
            another error), so the caller keeps the CID and can retry.
        """
        if not cid:
            return False
        try:
            self._run(["routing", "unpublish", cid])
            logger.info("ads_unpublish cid=%s server=%s", cid, self.server_addr)
            return True
        except DirectoryUnavailable as exc:
            if _RECORD_NOT_FOUND in str(exc).lower():
                logger.info("ads_unpublish_not_found cid=%s server=%s", cid, self.server_addr)
                return True
            logger.warning("ads_unpublish_failed cid=%s error=%s", cid, exc)
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _require_server_addr(self) -> None:
        if not self.server_addr:
            raise DirectoryUnavailable(
                "ADS server address is not configured. "
                "Pass server_addr= or set ADS_SERVER_ADDR / ADS_URL."
            )

    def _run(
        self, args: list[str], timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        self._require_server_addr()
        command = [self.dirctl_path, "--server-addr", self.server_addr, *args]
        if timeout is None:
            timeout = float(os.getenv("DIRCTL_TIMEOUT_SECONDS", "60"))
        try:
            result = subprocess.run(
                command, text=True, capture_output=True, check=False, timeout=timeout
            )
        except FileNotFoundError as exc:
            raise DirectoryUnavailable(
                f"dirctl not found at {self.dirctl_path!r}. "
                "Install it from https://github.com/agntcy/dir/releases."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DirectoryUnavailable(
                f"dirctl command timed out after {timeout:g}s: {' '.join(command)}"
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
