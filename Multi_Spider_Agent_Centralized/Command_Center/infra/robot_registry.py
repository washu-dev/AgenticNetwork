"""
Robot Registry: tracks robot state for CentralPiAgent instances.

Robots are discovered via ADS or static config (see discovery.py).
This registry maintains in-memory state: standing, busy, mission, capabilities.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import DEFAULT_CAMERA_PORT, HEALTH_CHECK_INTERVAL

logger = logging.getLogger(__name__)


_DEFAULT_DEG_PER_STEP = 30
_DEFAULT_ANGLE_PARAM = 25


def _extract_turn_calibration(capabilities: dict) -> tuple[int, int]:
    """Extract (deg_per_step, angle_param) from capabilities, with defaults."""
    cal = capabilities.get("turn_calibration", {})
    return (
        int(cal.get("deg_per_step", _DEFAULT_DEG_PER_STEP)),
        int(cal.get("angle_param", _DEFAULT_ANGLE_PARAM)),
    )


def _generate_actions_description(capabilities: dict) -> str:
    """Generate formatted actions description for LLM prompts.

    Same format as get_actions_description() in Multi_Spider_Agent/capabilities.py.
    """
    lines = []
    for a in capabilities.get("actions", []):
        line = f"- {a['name']}: {a['description']}"
        if a.get("requires_standing"):
            line += " (requires standing)"
        lines.append(line)
    lines.append("- MISSION_COMPLETE: Signal that the task goal has been fully accomplished")
    return "\n".join(lines)


@dataclass
class RegisteredRobot:
    robot_id: str
    host: str
    port: int = 9004                                     # Pi HTTP port
    camera_port: int = DEFAULT_CAMERA_PORT
    a2a_url: str = ""                                    # Robot Agent A2A HTTP endpoint
    robot_type: str = "spider"                           # spider / turtlebot / car
    capabilities: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)           # action name strings
    actions_description: str = ""                         # formatted for LLM
    deg_per_step: int = _DEFAULT_DEG_PER_STEP             # turn calibration: degrees per hardware step
    angle_param: int = _DEFAULT_ANGLE_PARAM               # turn calibration: leg swing parameter
    registered_at: float = 0.0
    last_heartbeat: float = 0.0
    status: str = "online"                                # online / offline
    zone: str = ""                                        # patrol zone (e.g. "A", "B")
    standing: bool = False
    busy: bool = False
    current_mission_id: Optional[str] = None
    # Position anchor from the most recent AprilTag sighting (world frame, meters)
    position: Optional[tuple] = None
    last_tag_id: Optional[int] = None
    position_updated_at: Optional[float] = None
    # Heading tracked via dead reckoning — cumulative turn since the robot
    # was placed at its zone's initial heading. Frontend adds this to the
    # zone's default heading to render the view cone. Turn LEFT decreases
    # (SVG/screen convention: 0=east, +90=south, -90=north).
    turn_offset_deg: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.robot_id,
            "robot_id": self.robot_id,
            "host": self.host,
            "port": self.port,
            "camera_port": self.camera_port,
            "robot_type": self.robot_type,
            "zone": self.zone,
            "actions": self.actions,
            "status": self.status,
            "standing": self.standing,
            "busy": self.busy,
            "current_mission_id": self.current_mission_id,
            "position": list(self.position) if self.position else None,
            "last_tag_id": self.last_tag_id,
            "position_updated_at": self.position_updated_at,
            "turn_offset_deg": self.turn_offset_deg,
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
        }


class RobotRegistry:
    """Manages robot state tracking."""

    def __init__(self):
        self._robots: dict[str, RegisteredRobot] = {}
        self._lock = asyncio.Lock()

    async def register(self, data: dict) -> dict:
        """Register a robot from direct POST or from discovery enrichment."""
        robot_id = data.get("robot_id", "")
        if not robot_id:
            return {"status": "error", "error": "robot_id required"}

        capabilities = data.get("capabilities", {})
        actions = [a["name"] for a in capabilities.get("actions", [])] if capabilities else []
        actions_description = _generate_actions_description(capabilities) if capabilities else ""
        deg_per_step, angle_param = _extract_turn_calibration(capabilities)

        now = time.time()
        async with self._lock:
            robot = RegisteredRobot(
                robot_id=robot_id,
                host=data.get("host", ""),
                port=data.get("port", 9004),
                camera_port=data.get("camera_port", DEFAULT_CAMERA_PORT),
                robot_type=data.get("robot_type", "spider"),
                capabilities=capabilities,
                actions=actions,
                actions_description=actions_description,
                deg_per_step=deg_per_step,
                angle_param=angle_param,
                zone=data.get("zone", ""),
                registered_at=now,
                last_heartbeat=now,
                status="online",
            )
            self._robots[robot_id] = robot

        logger.info(
            f"Robot registered: {robot_id} [{robot.robot_type}] at {robot.host}:{robot.port} "
            f"(camera={robot.camera_port}, zone={robot.zone}, {len(actions)} actions)"
        )
        return {"status": "registered", "robot_id": robot_id}

    def register_from_discovery(self, agent_info: dict):
        """Register/update a robot from discovery results (synchronous).

        Called after AgentDiscovery.get_online_agents() returns enriched agent info.
        If no capabilities provided, they will be fetched asynchronously from the Pi.
        """
        robot_id = agent_info.get("id", agent_info.get("agent_id", ""))
        if not robot_id:
            return

        capabilities = agent_info.get("capabilities") or {}
        actions = []
        actions_description = ""
        deg_per_step, angle_param = _DEFAULT_DEG_PER_STEP, _DEFAULT_ANGLE_PARAM
        if capabilities:
            actions = [a["name"] for a in capabilities.get("actions", [])]
            actions_description = _generate_actions_description(capabilities)
            deg_per_step, angle_param = _extract_turn_calibration(capabilities)

        zone = agent_info.get("zone", "")
        robot_type = agent_info.get("robot_type", "spider")

        now = time.time()
        a2a_url = agent_info.get("a2a_url", "")
        existing = self._robots.get(robot_id)
        if existing:
            # Update existing — keep state (standing, busy, mission)
            existing.host = agent_info.get("host", existing.host)
            existing.port = agent_info.get("port", existing.port)
            if a2a_url:
                existing.a2a_url = a2a_url
            existing.last_heartbeat = now
            existing.status = agent_info.get("status", "online")
            existing.robot_type = robot_type
            if zone:
                existing.zone = zone
            if capabilities:
                existing.capabilities = capabilities
                existing.actions = actions
                existing.actions_description = actions_description
                existing.deg_per_step = deg_per_step
                existing.angle_param = angle_param
        else:
            robot = RegisteredRobot(
                robot_id=robot_id,
                host=agent_info.get("host", ""),
                port=agent_info.get("port", 9004),
                camera_port=agent_info.get("camera_port", DEFAULT_CAMERA_PORT),
                a2a_url=a2a_url,
                robot_type=robot_type,
                capabilities=capabilities,
                actions=actions,
                actions_description=actions_description,
                deg_per_step=deg_per_step,
                angle_param=angle_param,
                zone=zone,
                registered_at=now,
                last_heartbeat=now if agent_info.get("status") == "online" else 0,
                status=agent_info.get("status", "online"),
            )
            self._robots[robot_id] = robot
            logger.info(f"Robot registered from discovery: {robot_id} [{robot_type}] (zone={zone}, status={robot.status}, a2a={a2a_url or 'default'})")

    async def _fetch_capabilities(self, robot_id: str, host: str, port: int):
        """Fetch capabilities from Pi's /capabilities endpoint and update registry.

        Raises on failure so callers know the robot is not ready for missions.
        """
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"http://{host}:{port}/capabilities")
                resp.raise_for_status()
                caps = resp.json()
                robot = self._robots.get(robot_id)
                if robot:
                    robot.capabilities = caps
                    robot.actions = [a["name"] for a in caps.get("actions", [])]
                    robot.actions_description = _generate_actions_description(caps)
                    robot.deg_per_step, robot.angle_param = _extract_turn_calibration(caps)
                    logger.info(f"Fetched capabilities for {robot_id}: {len(robot.actions)} actions, "
                                f"turn_cal=({robot.deg_per_step}°/step, angle={robot.angle_param})")
        except Exception as e:
            logger.error(f"Failed to fetch capabilities for {robot_id} at {host}:{port}: {e}")
            raise

    async def heartbeat(self, robot_id: str, data: dict = None) -> dict:
        """Update heartbeat. Called by Pi every 10s."""
        async with self._lock:
            robot = self._robots.get(robot_id)
            if not robot:
                return {"status": "error", "error": "robot not registered"}
            robot.last_heartbeat = time.time()
            robot.status = "online"
            if data:
                if "standing" in data:
                    robot.standing = data["standing"]
        return {"status": "ok"}

    async def unregister(self, robot_id: str) -> dict:
        """Remove a robot. Called by Pi on graceful shutdown."""
        async with self._lock:
            robot = self._robots.pop(robot_id, None)
        if robot:
            logger.info(f"Robot unregistered: {robot_id}")
            return {"status": "unregistered"}
        return {"status": "error", "error": "robot not found"}

    def get_online_robots(self) -> list[RegisteredRobot]:
        """Return robots with recent heartbeat."""
        now = time.time()
        return [
            r for r in self._robots.values()
            if r.status == "online"
            and (now - r.last_heartbeat) < HEALTH_CHECK_INTERVAL * 3
        ]

    def get_robot(self, robot_id: str) -> Optional[RegisteredRobot]:
        """Get a specific robot by ID."""
        return self._robots.get(robot_id)

    def get_all_robots(self) -> list[RegisteredRobot]:
        """Get all registered robots regardless of status."""
        return list(self._robots.values())

    def update_standing(self, robot_id: str, standing: bool):
        """Update robot's standing state after a command."""
        robot = self._robots.get(robot_id)
        if robot:
            robot.standing = standing

    def set_busy(self, robot_id: str, mission_id: str):
        """Mark robot as busy with a mission."""
        robot = self._robots.get(robot_id)
        if robot:
            robot.busy = True
            robot.current_mission_id = mission_id

    def clear_busy(self, robot_id: str):
        """Clear robot's busy state."""
        robot = self._robots.get(robot_id)
        if robot:
            robot.busy = False
            robot.current_mission_id = None

    def mark_offline(self, robot_id: str):
        """Mark robot as offline (heartbeat timeout)."""
        robot = self._robots.get(robot_id)
        if robot:
            robot.status = "offline"
            robot.busy = False
            robot.current_mission_id = None
            logger.warning(f"Robot marked offline: {robot_id}")


# Singleton
robot_registry = RobotRegistry()
