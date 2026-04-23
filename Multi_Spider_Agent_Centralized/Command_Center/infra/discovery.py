import asyncio
import logging
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from ..config import (
    PI_AGENTS,
    HEALTH_CHECK_TIMEOUT,
    DISCOVERY_MODE,
    ADS_SERVER_ADDRESS,
)

logger = logging.getLogger(__name__)

# ADS cache: avoid querying ADS on every request
_ads_cache: list[dict] = []
_ads_cache_time: float = 0
_ADS_CACHE_TTL = 60  # seconds


class AgentDiscovery:
    """Discover Robot Agents via ADS or static config.

    In the new architecture:
    - Robot Agents register themselves to ADS (A2A AgentCard + Pi info module)
    - Planner discovers Robot Agents via ADS
    - Planner communicates with Robot Agents via A2A JSON-RPC over HTTP
      (card ``url`` points to each Robot Agent's live A2A endpoint)
    - Robot Agents manage their own Physical Pi health checks

    Supports two modes (set via DISCOVERY_MODE env var):
    - "static": use hardcoded PI_AGENTS list from config
    - "ads": query Agent Directory Service for registered Robot Agents
    """

    def __init__(self, agents: Optional[list[dict]] = None):
        if agents:
            self.agents = agents
        elif DISCOVERY_MODE == "ads":
            self.agents = []
        else:
            self.agents = PI_AGENTS
        self._ads_client = None
        self._ads_connected = False

    async def health_check(self, host: str, port: int) -> Optional[dict]:
        """Check a Physical Pi's health via HTTP GET /health."""
        url = f"http://{host}:{port}/health"
        try:
            async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.debug(f"Health check failed for {host}:{port}: {e}")
            return None

    async def get_online_agents(self) -> list[dict]:
        """Discover Robot Agents and return those that are available.

        In ADS mode: queries ADS for registered Robot Agents.
        In static mode: uses PI_AGENTS list and health-checks Pi directly.
        """
        if DISCOVERY_MODE == "ads":
            ads_agents = await self._discover_from_ads()
            if ads_agents:
                self.agents = ads_agents
                # Health-check each Pi to determine actual status
                tasks = [self._check_agent_ads(a) for a in ads_agents]
                results = await asyncio.gather(*tasks)
                return [r for r in results if r is not None]
            else:
                logger.warning("ADS returned no agents, falling back to static config")

        # Static mode: health-check Pi directly
        tasks = [self._check_agent_static(a) for a in self.agents]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]

    async def _check_agent_ads(self, agent: dict) -> Optional[dict]:
        """ADS mode: return agent info, health-check Pi to determine status.

        ``agent["host"]`` / ``agent["port"]`` are the *Pi* endpoint
        (populated by ``_discover_from_ads`` from the multi_spider/pi_info
        module).  ``agent["a2a_url"]`` is the A2A HTTP endpoint.
        """
        health = await self.health_check(agent["host"], agent["port"])
        status = "online" if health and health.get("status") == "ok" else "offline"
        return {
            "id": agent["id"],
            "host": agent.get("host", ""),
            "port": agent.get("port", 9004),
            "camera_port": agent.get("camera_port", 9000),
            "a2a_url": agent.get("a2a_url", ""),
            "zone": agent.get("zone", ""),
            "robot_type": agent.get("robot_type", "spider"),
            "status": status,
        }

    async def _check_agent_static(self, agent: dict) -> Optional[dict]:
        """Static mode: health-check a Pi directly and synthesise an A2A URL.

        In static mode the Robot Agent process is assumed to run on the
        same host as the planner (``PLANNER_A2A_HOST_DEFAULT``) with a
        deterministic port derived from the robot_id.
        """
        health = await self.health_check(agent["host"], agent["port"])
        if health and health.get("status") == "ok":
            # Import lazily to avoid circular import at module load
            from ..robot_a2a import a2a_http_port
            import os
            a2a_host = os.getenv("PLANNER_A2A_HOST_DEFAULT", "localhost")
            return {
                "id": agent["id"],
                "host": agent["host"],
                "port": agent["port"],
                "camera_port": agent.get("camera_port", 9000),
                "a2a_url": f"http://{a2a_host}:{a2a_http_port(agent['id'])}",
                "zone": agent.get("zone", ""),
                "robot_type": agent.get("robot_type", "spider"),
                "agent_id": health.get("agent_id", agent["id"]),
                "status": "online",
            }
        return None

    # ------------------------------------------------------------------
    # ADS Discovery (with cache)
    # ------------------------------------------------------------------

    async def _discover_from_ads(self) -> list[dict]:
        """Query ADS for registered PiCrawler agents.

        Results are cached for 60 seconds to avoid querying ADS on every request.
        """
        global _ads_cache, _ads_cache_time

        now = time.time()
        if _ads_cache and (now - _ads_cache_time) < _ADS_CACHE_TTL:
            return _ads_cache

        try:
            from agntcy.dir_sdk.client import Client, Config
            from agntcy.dir_sdk.models import search_v1
            from google.protobuf.json_format import MessageToDict
        except ImportError:
            logger.error(
                "agntcy-dir SDK not installed. "
                "Install with: pip install agntcy-dir"
            )
            return []

        try:
            config = Config(server_address=ADS_SERVER_ADDRESS)
            client = Client(config)
        except Exception as e:
            logger.error(f"Failed to connect to ADS at {ADS_SERVER_ADDRESS}: {e}")
            return []

        try:
            search_req = search_v1.SearchRecordsRequest(limit=20)
            responses = client.search_records(search_req)

            if not responses:
                return []

            # Pass 1: collect all records per agent_id, so we can prefer
            # "modern" records (ones with multi_spider/pi_info or a non-legacy
            # url) over stale pre-A2A records left in ADS.
            collected: dict[str, list[dict]] = {}
            for resp in responses:
                if not (resp.record and resp.record.data):
                    continue
                record = MessageToDict(resp.record.data)
                tags = self._extract_tags(record)
                if not any(t in tags for t in ("crawler", "patrol", "robot", "ground", "spider", "turtlebot", "car")):
                    continue
                agent_id_preview = self._extract_agent_id(record) or ""
                if not agent_id_preview:
                    continue
                collected.setdefault(agent_id_preview, []).append(record)

            def _prefer_modern(record: dict) -> int:
                """Higher score = more modern (preferred)."""
                score = 0
                for mod in record.get("modules", []):
                    if mod.get("name") == "multi_spider/pi_info":
                        score += 2  # new-format record
                    if mod.get("name") == "integration/a2a":
                        card = mod.get("data", {}).get("card_data", {})
                        if card.get("additionalInterfaces"):
                            score += 2  # multi-transport card
                        if str(card.get("url", "")).startswith(("nats://", "slim://")):
                            score += 1
                return score

            agents = []
            seen_ids: set[str] = set()

            # Flatten in preference order (most modern first per agent_id)
            ordered_records: list[dict] = []
            for aid, recs in collected.items():
                recs.sort(key=_prefer_modern, reverse=True)
                ordered_records.extend(recs)

            for record in ordered_records:
                tags = self._extract_tags(record)

                # A2A endpoint URL from the card
                a2a_url = self._extract_url(record)
                if not a2a_url:
                    continue

                # Pi physical info from the non-standard pi_info module
                pi_info = self._extract_pi_info(record)
                pi_host = pi_info.get("pi_host", "")
                pi_port = int(pi_info.get("pi_port", 9004))
                camera_port = int(pi_info.get("camera_port", 9000))
                if not pi_host:
                    # Back-compat with pre-A2A records where card.url
                    # still encoded the Pi endpoint.
                    parsed = urlparse(a2a_url)
                    pi_host = parsed.hostname or ""

                # Extract agent ID
                agent_id = self._extract_agent_id(record)
                if not agent_id or agent_id in seen_ids:
                    continue
                seen_ids.add(agent_id)

                zone = ""
                for t in tags:
                    if t.startswith("zone-"):
                        zone = t[5:].upper()
                        break

                robot_type = "spider"
                for t in tags:
                    if t in ("spider", "turtlebot", "car"):
                        robot_type = t
                        break

                agents.append({
                    "id": agent_id,
                    "host": pi_host,
                    "port": pi_port,
                    "camera_port": camera_port,
                    "a2a_url": a2a_url,
                    "zone": zone,
                    "robot_type": robot_type,
                })

            if agents:
                _ads_cache = agents
                _ads_cache_time = now
                logger.info(f"ADS discovered {len(agents)} agent(s): {[a['id'] for a in agents]}")

            return agents

        except Exception as e:
            logger.error(f"ADS query failed: {e}")
            return []

    @staticmethod
    def _extract_url(record: dict) -> str:
        """Extract the HTTP A2A URL from an OASF record.

        The planner needs an HTTP URL to fetch the AgentCard via
        ``A2ACardResolver``.  When the card advertises multiple
        transports (additional_interfaces), we prefer the HTTP one
        regardless of the card's top-level ``preferred_transport``.
        """
        # Annotations (legacy)
        url = record.get("annotations", {}).get("a2a.url", "")
        if url.startswith("http"):
            return url

        # Prefer the HTTP interface from additional_interfaces, so we can
        # always resolve the card even when preferred_transport is NATS/SLIM.
        for module in record.get("modules", []):
            if module.get("name") != "integration/a2a":
                continue
            card = module.get("data", {}).get("card_data", {})
            additional = card.get("additionalInterfaces") or card.get("additional_interfaces") or []
            for iface in additional:
                transport = str(iface.get("transport", "")).lower()
                iface_url = iface.get("url", "")
                if transport in ("jsonrpc", "http") and iface_url.startswith("http"):
                    return iface_url

        # Fall back to card_data.url regardless of scheme (best-effort)
        for module in record.get("modules", []):
            if module.get("name") == "integration/a2a":
                data = module.get("data", {})
                url = data.get("card_data", {}).get("url", "") or data.get("url", "")
                if url:
                    return url

        return ""

    @staticmethod
    def _extract_pi_info(record: dict) -> dict:
        """Pull Pi host/port/camera_port from the multi_spider/pi_info module (if any)."""
        for module in record.get("modules", []):
            if module.get("name") == "multi_spider/pi_info":
                data = module.get("data") or {}
                if isinstance(data, dict):
                    return data
        return {}

    @staticmethod
    def _extract_agent_id(record: dict) -> str:
        """Extract agent ID from OASF record.

        Priority: card_data.name parse -> card_data.id -> record name fallback.
        For PiCrawler cards, the name format is "Crawler Agent - {agent_id}",
        so we extract the part after the last " - ".
        """
        for module in record.get("modules", []):
            if module.get("name") == "integration/a2a":
                card_data = module.get("data", {}).get("card_data", {})

                # Try to extract from card name: "Crawler Agent - crawler-alpha-patrol-001"
                name = card_data.get("name", "")
                if " - " in name:
                    return name.split(" - ", 1)[1].strip()

                # Try card_data.id
                aid = card_data.get("id", "")
                if aid:
                    return aid

        # Fall back to record name
        name = record.get("name", "")
        if " - " in name:
            return name.split(" - ", 1)[1].strip()
        if name:
            return name.replace(" ", "-").lower()
        return ""

    @staticmethod
    def _extract_tags(record: dict) -> list[str]:
        """Extract all tags from OASF record."""
        tags = []

        # Tags from card_data.skills
        for module in record.get("modules", []):
            if module.get("name") == "integration/a2a":
                card_data = module.get("data", {}).get("card_data", {})
                for skill in card_data.get("skills", []):
                    tags.extend(skill.get("tags", []))

        # Tags from top-level OASF skills
        for skill in record.get("skills", []):
            if isinstance(skill, dict) and "tags" in skill:
                tags.extend(skill["tags"])

        return [t.lower() for t in tags]
