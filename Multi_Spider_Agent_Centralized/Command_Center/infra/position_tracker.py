"""
Position tracker and zone adjacency graph.

Maintains each robot's last-known world position via AprilTag observations,
and serves the zone adjacency graph used by support-dispatch logic.
"""

import json
import logging
import math
import time
from pathlib import Path
from typing import Optional

from .robot_registry import robot_registry

logger = logging.getLogger(__name__)


_zone_adjacency: dict[str, list[str]] = {}


def _adjacency_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config_data" / "zone_adjacency.json"


def load_zone_adjacency(path: Optional[Path] = None) -> dict:
    """Load zone adjacency graph from JSON."""
    global _zone_adjacency
    cfg_path = path or _adjacency_path()
    try:
        with open(cfg_path) as f:
            raw = json.load(f)
        _zone_adjacency = {
            k: list(v) for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, list)
        }
        logger.info(f"Loaded zone adjacency: {list(_zone_adjacency.keys())}")
    except FileNotFoundError:
        logger.warning(f"Zone adjacency config not found at {cfg_path}")
        _zone_adjacency = {}
    except Exception as e:
        logger.error(f"Failed to load zone adjacency: {e}")
        _zone_adjacency = {}
    return _zone_adjacency


def get_neighbors(zone: str) -> list[str]:
    """Return adjacent zones for a given zone."""
    return list(_zone_adjacency.get(zone, []))


def are_adjacent(zone_a: str, zone_b: str) -> bool:
    if zone_a == zone_b:
        return True
    return zone_b in _zone_adjacency.get(zone_a, [])


def update_robot_position(robot_id: str, tag_id: int, pos: tuple[float, float], zone: Optional[str] = None):
    """Record a position anchor from an AprilTag sighting."""
    robot = robot_registry.get_robot(robot_id)
    if not robot:
        logger.debug(f"Cannot update position: robot {robot_id} not in registry")
        return
    robot.position = tuple(pos)
    robot.last_tag_id = int(tag_id)
    robot.position_updated_at = time.time()
    if zone and zone != robot.zone:
        if robot.zone:
            logger.info(f"Robot {robot_id} moved from zone {robot.zone} to {zone}")
        robot.zone = zone
    logger.debug(f"Position update: {robot_id} → tag#{tag_id} @ {pos}")


def get_position(robot_id: str) -> Optional[tuple[float, float]]:
    robot = robot_registry.get_robot(robot_id)
    if robot is None:
        return None
    return robot.position


def euclidean(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    dx = p1[0] - p2[0]
    dy = p1[1] - p2[1]
    return math.sqrt(dx * dx + dy * dy)


def find_nearest_robot(target_pos: tuple[float, float],
                       exclude_ids: Optional[list[str]] = None,
                       require_idle: bool = True) -> Optional[dict]:
    """Return {robot_id, robot, distance} for the nearest eligible robot, or None.

    A robot is eligible if:
      - It has a known position.
      - It is online.
      - If require_idle=True, it is not busy.
      - Its robot_id is not in exclude_ids.
    """
    exclude = set(exclude_ids or [])
    candidates = []

    for robot in robot_registry.get_online_robots():
        if robot.robot_id in exclude:
            continue
        if require_idle and robot.busy:
            continue
        if robot.position is None:
            continue
        dist = euclidean(robot.position, target_pos)
        candidates.append((dist, robot))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    dist, robot = candidates[0]
    return {"robot_id": robot.robot_id, "robot": robot, "distance": dist}


# Load on import
load_zone_adjacency()
