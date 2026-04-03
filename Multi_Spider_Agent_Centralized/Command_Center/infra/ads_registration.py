"""
ADS Registration for Robot Agents (server-side).

Registers Robot Agent identity to the Agent Directory Service.
Moved from Pi's card.py — registration now happens on Command Center.
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
    host: str,
    port: int,
    ads_address: str,
    oasf_address: str = "",
):
    """Register a Robot Agent to ADS.

    Args:
        robot_id: Robot ID (e.g. "crawler-alpha-patrol-004")
        robot_type: Robot type (spider, turtlebot, car)
        zone: Patrol zone (A, B, ...)
        host: Pi host IP (for stream URL)
        port: Pi A2A port
        ads_address: ADS gRPC address
        oasf_address: OASF translation service address (optional)
    """
    if not ads_address:
        logger.info("No ADS address configured, skipping registration")
        return

    # Build agent card data
    card_data = _build_agent_card(robot_id, robot_type, zone, host, port)
    tags = [robot_type, "robot", "patrol", "agent", "ground"]
    if zone:
        tags.append(f"zone-{zone.lower()}")

    # Check cache — skip if unchanged
    _ADS_CACHE_DIR.mkdir(exist_ok=True)
    cache_file = _ADS_CACHE_DIR / f"{robot_id}.json"
    current_hash = hashlib.sha256(json.dumps(card_data, sort_keys=True).encode()).hexdigest()[:16]

    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
            if cache.get("hash") == current_hash:
                logger.info(f"ADS: {robot_id} unchanged, skipping (CID={cache.get('cid', '?')})")
                return
        except (json.JSONDecodeError, KeyError):
            pass

    try:
        import grpc
        from google.protobuf.json_format import ParseDict
        from google.protobuf.struct_pb2 import Struct
        from agntcy.dir_sdk.client import Client, Config
        from agntcy.dir_sdk.models import core_v1, routing_v1
    except ImportError as e:
        logger.warning(f"ADS SDK not installed, skipping registration: {e}")
        return

    try:
        from datetime import datetime

        # Build OASF record directly (no translation service needed)
        record_dict = {
            "schema_version": "0.8.0",
            "version": "4.0.0",
            "name": f"Robot Agent - {robot_id}",
            "description": card_data["description"],
            "authors": ["Multi-Spider Command Center"],
            "created_at": datetime.utcnow().isoformat() + "Z",
            "skills": [{"name": "advanced_reasoning_planning/strategic_planning", "id": 1501}],
            "domains": [{"name": "technology/software_engineering", "id": 102}],
            "modules": [{
                "name": "integration/a2a",
                "data": {
                    "card_data": card_data,
                    "protocol_version": "0.2.3",
                },
            }],
        }

        data_struct = Struct()
        ParseDict(record_dict, data_struct)
        record = core_v1.Record(data=data_struct)

        config = Config(server_address=ads_address)
        client = Client(config)

        # Remove old record if exists
        if cache_file.exists():
            try:
                old_cache = json.loads(cache_file.read_text())
                old_cid = old_cache.get("cid")
                if old_cid:
                    old_ref = core_v1.RecordRef(cid=old_cid)
                    old_refs = routing_v1.RecordRefs(refs=[old_ref])
                    client.unpublish(routing_v1.UnpublishRequest(record_refs=old_refs))
                    client.delete([old_ref])
                    logger.info(f"ADS: removed old record for {robot_id} CID={old_cid}")
            except Exception as e:
                logger.debug(f"ADS: old record cleanup failed (non-fatal): {e}")

        refs = client.push([record])
        cid = refs[0].cid

        record_refs = routing_v1.RecordRefs(refs=[core_v1.RecordRef(cid=cid)])
        client.publish(routing_v1.PublishRequest(record_refs=record_refs))

        # Save cache
        cache_file.write_text(json.dumps({"hash": current_hash, "cid": cid}))
        logger.info(f"ADS: registered {robot_id} CID={cid}")

    except Exception as e:
        logger.warning(f"ADS registration failed for {robot_id}: {e}")


def _build_agent_card(robot_id: str, robot_type: str, zone: str, host: str, port: int) -> dict:
    """Build A2A-compatible agent card for ADS."""
    zone_desc = f" (Zone {zone})" if zone else ""
    type_names = {"spider": "PiCrawler Spider", "turtlebot": "TurtleBot", "car": "Car Robot"}
    type_name = type_names.get(robot_type, robot_type)

    return {
        "name": f"Robot Agent - {robot_id}",
        "id": robot_id,
        "description": (
            f"{type_name} robot agent{zone_desc}. "
            f"Managed by Command Center Robot Agent service. "
            f"Physical robot at {host}:{port}."
        ),
        "url": f"http://{host}:{port}",
        "version": "4.0.0",
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text", "application/json"],
        "capabilities": {"streaming": False},
        "skills": [{
            "id": "ground_patrol",
            "name": f"{type_name} Patrol",
            "description": f"{type_name} agent for zone patrol and detection.",
            "tags": [robot_type, "robot", "patrol", "agent", "ground", f"zone-{zone.lower()}" if zone else "unzoned"],
        }],
    }
