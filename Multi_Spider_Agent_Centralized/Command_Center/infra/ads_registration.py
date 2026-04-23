"""
ADS Registration for Robot Agents (server-side).

Registers a Robot Agent's A2A AgentCard into the Agent Directory Service.

The card's ``url`` points to the Robot Agent's live A2A HTTP endpoint so
any A2A-compliant client can discover and call it.  Pi-specific details
(Pi host/port, camera port) are stored alongside the card inside the OASF
record module data, so the planner can still reach the Pi for legacy
operations without those fields leaking into the A2A Card itself.
"""

import json
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_ADS_CACHE_DIR = Path(__file__).parent.parent / ".ads_cache"


def register_robot_agent_to_ads(
    robot_id: str,
    robot_type: str,
    zone: str,
    pi_host: str,
    pi_port: int,
    camera_port: int,
    a2a_host: str,
    a2a_port: int,
    ads_address: str,
    transport_plan=None,
):
    """Register a Robot Agent to ADS.

    Args:
        robot_id: stable robot id
        robot_type: spider / turtlebot / car
        zone: patrol zone (may be empty)
        pi_host: Pi host (physical robot)
        pi_port: Pi HTTP port (for commands)
        camera_port: Pi camera port (UDP video push)
        a2a_host: host of the Robot Agent's A2A HTTP server
        a2a_port: port of the Robot Agent's A2A HTTP server
        ads_address: ADS gRPC address (empty → skip registration)
        transport_plan: optional ``robot_a2a.card.TransportPlan`` describing
            which transports are active.  If provided, card_data reflects
            the full multi-transport card (including additional_interfaces);
            otherwise a single-transport HTTP-only card is produced.
    """
    if not ads_address:
        logger.info("No ADS address configured, skipping registration")
        return

    if transport_plan is not None:
        card_data = _card_data_from_plan(robot_id, robot_type, zone, transport_plan)
    else:
        card_data = _build_agent_card_data(robot_id, robot_type, zone, a2a_host, a2a_port)
    pi_info = {
        "pi_host": pi_host,
        "pi_port": pi_port,
        "camera_port": camera_port,
    }
    tags = [robot_type, "robot", "patrol", "agent", "ground"]
    if zone:
        tags.append(f"zone-{zone.lower()}")

    _ADS_CACHE_DIR.mkdir(exist_ok=True)
    cache_file = _ADS_CACHE_DIR / f"{robot_id}.json"
    digest_source = {"card_data": card_data, "pi_info": pi_info}
    current_hash = hashlib.sha256(
        json.dumps(digest_source, sort_keys=True).encode()
    ).hexdigest()[:16]

    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
            if cache.get("hash") == current_hash:
                logger.info(
                    f"ADS: {robot_id} unchanged, skipping (CID={cache.get('cid', '?')})"
                )
                return
        except (json.JSONDecodeError, KeyError):
            pass

    try:
        from google.protobuf.json_format import ParseDict
        from google.protobuf.struct_pb2 import Struct
        from agntcy.dir_sdk.client import Client, Config
        from agntcy.dir_sdk.models import core_v1, routing_v1
    except ImportError as e:
        logger.warning(f"ADS SDK not installed, skipping registration: {e}")
        return

    try:
        from datetime import datetime

        record_dict = {
            "schema_version": "0.8.0",
            "version": "4.0.0",
            "name": f"Robot Agent - {robot_id}",
            "description": card_data["description"],
            "authors": ["Multi-Spider Command Center"],
            "created_at": datetime.utcnow().isoformat() + "Z",
            "skills": [{"name": "advanced_reasoning_planning/strategic_planning", "id": 1501}],
            "domains": [{"name": "technology/software_engineering", "id": 102}],
            "modules": [
                {
                    "name": "integration/a2a",
                    "data": {
                        "card_data": card_data,
                        "protocol_version": "0.3.0",
                    },
                },
                # Non-standard extension: Pi-physical info, consumed only
                # by this project's discovery.py.  Kept separate so the
                # A2A card remains clean/portable.
                {
                    "name": "multi_spider/pi_info",
                    "data": pi_info,
                },
            ],
        }

        data_struct = Struct()
        ParseDict(record_dict, data_struct)
        record = core_v1.Record(data=data_struct)

        config = Config(server_address=ads_address)
        client = Client(config)

        # Remove any old record for this robot — via local cache first, then
        # via ADS search (handles the case where the local cache was wiped
        # but ADS still has prior records under the same agent name).
        _unpublish_prior_records(client, robot_id, cache_file, record_dict["name"])

        refs = client.push([record])
        cid = refs[0].cid

        record_refs = routing_v1.RecordRefs(refs=[core_v1.RecordRef(cid=cid)])
        client.publish(routing_v1.PublishRequest(record_refs=record_refs))

        cache_file.write_text(json.dumps({"hash": current_hash, "cid": cid}))
        logger.info(f"ADS: registered {robot_id} CID={cid}")

    except Exception as e:
        logger.warning(f"ADS registration failed for {robot_id}: {e}")


def _unpublish_prior_records(client, robot_id: str, cache_file, record_name: str) -> None:
    """Remove prior ADS records for this robot.

    First honours ``cache_file.cid`` (fast path after normal lifecycle),
    then searches ADS by ``record.name`` to catch records we no longer
    have the CID for (e.g. after a clean cache or different code version).
    Scope is strictly limited to records whose ``name`` exactly matches
    the one we're about to publish — never touches anyone else's records.
    """
    from agntcy.dir_sdk.models import core_v1, routing_v1

    removed_cids: set[str] = set()

    if cache_file.exists():
        try:
            old_cache = json.loads(cache_file.read_text())
            old_cid = old_cache.get("cid")
            if old_cid:
                try:
                    old_ref = core_v1.RecordRef(cid=old_cid)
                    old_refs = routing_v1.RecordRefs(refs=[old_ref])
                    client.unpublish(routing_v1.UnpublishRequest(record_refs=old_refs))
                    client.delete([old_ref])
                    removed_cids.add(old_cid)
                    logger.info(f"ADS: removed cached record for {robot_id} CID={old_cid[:16]}…")
                except Exception as e:
                    logger.debug(f"ADS: cached-CID cleanup failed (non-fatal): {e}")
        except Exception:
            pass

    try:
        from agntcy.dir_sdk.models import search_v1
        from google.protobuf.json_format import MessageToDict

        req = search_v1.SearchRecordsRequest(limit=50)
        for resp in client.search_records(req):
            if not (resp.record and resp.record.data):
                continue
            rec = MessageToDict(resp.record.data)
            if rec.get("name") != record_name:
                continue
            cid = getattr(getattr(resp, "record_ref", None), "cid", "")
            if not cid or cid in removed_cids:
                continue
            try:
                ref = core_v1.RecordRef(cid=cid)
                refs = routing_v1.RecordRefs(refs=[ref])
                client.unpublish(routing_v1.UnpublishRequest(record_refs=refs))
                client.delete([ref])
                removed_cids.add(cid)
                logger.info(
                    f"ADS: removed stale record for {robot_id} CID={cid[:16]}… (name match)"
                )
            except Exception as e:
                logger.debug(f"ADS: stale-record cleanup failed for {cid[:16]}: {e}")
    except Exception as e:
        logger.debug(f"ADS: search pass skipped: {e}")


def _card_data_from_plan(robot_id: str, robot_type: str, zone: str, plan) -> dict:
    """Build card_data dict from a TransportPlan by serialising the actual AgentCard."""
    from ..robot_a2a.card import build_agent_card
    card = build_agent_card(robot_id, robot_type, zone, plan)
    data = card.model_dump(mode="json", exclude_none=True, by_alias=True)
    # Stamp protocolVersion explicitly for OASF readers.
    data.setdefault("protocolVersion", "0.3.0")
    return data


def _build_agent_card_data(
    robot_id: str, robot_type: str, zone: str, a2a_host: str, a2a_port: int
) -> dict:
    """Build A2A Card payload (as dict).  Mirrors robot_a2a.card.build_agent_card."""
    zone_desc = f" (Zone {zone})" if zone else ""
    type_names = {"spider": "PiCrawler Spider", "turtlebot": "TurtleBot", "car": "Car Robot"}
    type_name = type_names.get(robot_type, robot_type)

    return {
        "name": f"Robot Agent - {robot_id}",
        "description": (
            f"{type_name} robot agent{zone_desc}. "
            f"Physical robot controller operating under Command Center."
        ),
        "url": f"http://{a2a_host}:{a2a_port}",
        "version": "4.0.0",
        "protocolVersion": "0.3.0",
        "preferredTransport": "JSONRPC",
        "defaultInputModes": ["text", "application/json"],
        "defaultOutputModes": ["text", "application/json"],
        "capabilities": {"streaming": False},
        "skills": [
            {
                "id": "ground_patrol",
                "name": f"{type_name} Patrol",
                "description": (
                    f"{type_name} agent for zone patrol, object detection, "
                    f"VLM scene description, and direct movement commands."
                ),
                "tags": [
                    robot_type, "robot", "patrol", "agent", "ground",
                    f"zone-{zone.lower()}" if zone else "unzoned",
                ],
            }
        ],
    }
