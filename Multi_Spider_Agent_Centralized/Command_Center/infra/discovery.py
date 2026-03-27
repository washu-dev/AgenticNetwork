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
    """Discover and health-check PiCrawler agents on the network.

    Supports two modes (set via DISCOVERY_MODE env var):
    - "static": use hardcoded PI_AGENTS list from config
    - "ads": query Agent Directory Service for registered agents, then health-check
    """

    def __init__(self, agents: Optional[list[dict]] = None):
        if agents:
            self.agents = agents
        elif DISCOVERY_MODE == "ads":
            self.agents = []  # ADS mode: populated by _discover_from_ads()
        else:
            self.agents = PI_AGENTS
        self._ads_client = None
        self._ads_connected = False

    async def health_check(self, host: str, port: int) -> Optional[dict]:
        """Check a single agent's health. Returns response dict or None if unreachable."""
        url = f"http://{host}:{port}/health"
        try:
            async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.debug(f"Health check failed for {host}:{port}: {e}")
            return None

    async def get_agent_card(self, host: str, port: int) -> Optional[dict]:
        """Fetch agent card from /.well-known/agent-card.json."""
        url = f"http://{host}:{port}/.well-known/agent-card.json"
        try:
            async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.debug(f"Agent card fetch failed for {host}:{port}: {e}")
            return None

    async def get_capabilities(self, host: str, port: int) -> Optional[dict]:
        """Fetch capabilities from GET /capabilities."""
        url = f"http://{host}:{port}/capabilities"
        try:
            async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.debug(f"Capabilities fetch failed for {host}:{port}: {e}")
            return None

    async def get_online_agents(self) -> list[dict]:
        """Health-check all agents, return those that are online.

        In ADS mode, first queries ADS to discover agent URLs,
        then health-checks each discovered agent.
        """
        if DISCOVERY_MODE == "ads":
            ads_agents = await self._discover_from_ads()
            if ads_agents:
                self.agents = ads_agents
            else:
                logger.warning("ADS returned no agents, falling back to static config")

        tasks = []
        for agent in self.agents:
            tasks.append(self._check_agent(agent))
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]

    async def _check_agent(self, agent: dict) -> Optional[dict]:
        """Check one agent and return enriched info if online."""
        health = await self.health_check(agent["host"], agent["port"])
        if health and health.get("status") == "ok":
            # Fetch capabilities for LLM prompts
            caps = await self.get_capabilities(agent["host"], agent["port"])
            return {
                "id": agent["id"],
                "host": agent["host"],
                "port": agent["port"],
                "agent_id": health.get("agent_id", agent["id"]),
                "uptime_s": health.get("uptime_s"),
                "busy": health.get("busy", False),
                "current_task": health.get("current_task"),
                "capabilities": caps,
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

            agents = []
            seen_ids = set()

            for resp in responses:
                if not (resp.record and resp.record.data):
                    continue

                record = MessageToDict(resp.record.data)

                # Filter: only PiCrawler agents (check tags for "crawler")
                tags = self._extract_tags(record)
                if not any(t in tags for t in ("crawler", "patrol", "robot", "ground")):
                    continue

                # Extract URL from record
                url = self._extract_url(record)
                if not url:
                    continue

                # Parse URL into host/port
                parsed = urlparse(url)
                host = parsed.hostname
                port = parsed.port or 9004

                # Extract agent ID
                agent_id = self._extract_agent_id(record)
                if not agent_id or agent_id in seen_ids:
                    continue
                seen_ids.add(agent_id)

                agents.append({
                    "id": agent_id,
                    "host": host,
                    "port": port,
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
        """Extract agent URL from OASF record."""
        # Try annotations first
        url = record.get("annotations", {}).get("a2a.url", "")
        if url:
            return url

        # Try card_data in modules
        for module in record.get("modules", []):
            if module.get("name") == "integration/a2a":
                data = module.get("data", {})
                url = data.get("card_data", {}).get("url", "")
                if url:
                    return url
                url = data.get("url", "")
                if url:
                    return url

        return ""

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
