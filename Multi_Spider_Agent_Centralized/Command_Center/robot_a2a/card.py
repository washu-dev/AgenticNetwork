"""AgentCard builder for Robot Agents.

Supports multi-transport serving: each Robot Agent can be reached over
HTTP / NATS patterns / SLIM patterns simultaneously.  The card's
``preferred_transport`` tells A2A clients which one to pick by default;
``additional_interfaces`` list the others so clients can fall back.

Topic / identity conventions (matching v1_classification):
  - NATS patterns: ``agents.direct.{robot_id}``
  - SLIM patterns: ``default/default/{robot_id}``
  - HTTP:          ``http://{a2a_host}:{a2a_port}``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TransportProtocol,
)

_TYPE_NAMES = {
    "spider": "PiCrawler Spider",
    "turtlebot": "TurtleBot",
    "car": "Car Robot",
}


# Canonical identifier strings used in InterfaceTransport.
_IFACE_JSONRPC = "jsonrpc"
_IFACE_NATS = "natspatterns"
_IFACE_SLIM = "slimpatterns"


@dataclass(frozen=True)
class TransportPlan:
    """Which transports this Robot Agent should serve.

    Exactly one of ``enable_http`` / ``enable_nats`` / ``enable_slim`` is
    treated as the "preferred" transport (the one written into
    ``card.url`` / ``card.preferred_transport``).  The others appear in
    ``additional_interfaces``.
    """
    preferred: str  # "HTTP" | "NATS" | "SLIM"
    enable_http: bool
    enable_nats: bool
    enable_slim: bool

    a2a_host: str
    a2a_port: int

    nats_endpoint: str
    slim_endpoint: str

    def __post_init__(self):
        if self.preferred not in ("HTTP", "NATS", "SLIM"):
            raise ValueError(f"preferred must be HTTP/NATS/SLIM, got {self.preferred!r}")
        if self.preferred == "HTTP" and not self.enable_http:
            raise ValueError("preferred=HTTP but enable_http=False")
        if self.preferred == "NATS" and not self.enable_nats:
            raise ValueError("preferred=NATS but enable_nats=False")
        if self.preferred == "SLIM" and not self.enable_slim:
            raise ValueError("preferred=SLIM but enable_slim=False")


def nats_topic_for(robot_id: str) -> str:
    """NATS patterns topic: matches v1's ``agents.direct.{id}`` convention."""
    return f"agents.direct.{robot_id}"


def slim_identity_for(robot_id: str) -> str:
    """SLIM patterns routable name: matches v1's ``default/default/{id}`` convention."""
    return f"default/default/{robot_id}"


def _build_interface(transport: str, url: str) -> AgentInterface:
    return AgentInterface(transport=transport, url=url)


def _preferred_url(plan: TransportPlan, robot_id: str) -> tuple[str, str]:
    """Return (preferred_transport_enum_value, url) for the card's top-level fields."""
    if plan.preferred == "HTTP":
        return _IFACE_JSONRPC, f"http://{plan.a2a_host}:{plan.a2a_port}"
    if plan.preferred == "NATS":
        # Explicit-endpoint NATS URL so clients can reach the broker without
        # needing an env-var override.
        return _IFACE_NATS, f"{plan.nats_endpoint}/{nats_topic_for(robot_id)}"
    # SLIM
    return _IFACE_SLIM, f"{plan.slim_endpoint}/{slim_identity_for(robot_id)}"


def build_agent_skill(robot_type: str, zone: str) -> AgentSkill:
    type_name = _TYPE_NAMES.get(robot_type, robot_type)
    tags = [robot_type, "robot", "patrol", "agent", "ground"]
    tags.append(f"zone-{zone.lower()}" if zone else "unzoned")
    return AgentSkill(
        id="ground_patrol",
        name=f"{type_name} Patrol",
        description=(
            f"{type_name} agent for zone patrol, object detection, VLM scene "
            f"description, and direct movement commands."
        ),
        tags=tags,
        examples=[
            "Patrol the zone and report anything unusual",
            "Find the blue cone and report",
            "Watch for people entering the area and raise an alarm",
            "Move forward 5 steps then turn right",
        ],
    )


def build_agent_card(
    robot_id: str,
    robot_type: str,
    zone: str,
    plan: TransportPlan,
) -> AgentCard:
    """Build an A2A AgentCard with multi-transport interfaces.

    Args:
        robot_id: stable robot identifier
        robot_type: spider / turtlebot / car
        zone: patrol zone string (may be empty)
        plan: describes which transports to serve, their endpoints, and
            which one is the default ``preferred_transport``.
    """
    type_name = _TYPE_NAMES.get(robot_type, robot_type)
    zone_desc = f" (Zone {zone})" if zone else ""

    # Build AdditionalInterfaces for every enabled transport.  The
    # preferred transport still lives in the top-level ``url`` /
    # ``preferred_transport`` fields; other enabled transports are
    # advertised as additional interfaces so clients can negotiate.
    preferred_transport, preferred_url = _preferred_url(plan, robot_id)

    interfaces: list[AgentInterface] = []

    if plan.enable_http:
        http_url = f"http://{plan.a2a_host}:{plan.a2a_port}"
        interfaces.append(_build_interface(_IFACE_JSONRPC, http_url))

    if plan.enable_nats:
        nats_url = f"{plan.nats_endpoint}/{nats_topic_for(robot_id)}"
        interfaces.append(_build_interface(_IFACE_NATS, nats_url))

    if plan.enable_slim:
        slim_url = f"{plan.slim_endpoint}/{slim_identity_for(robot_id)}"
        interfaces.append(_build_interface(_IFACE_SLIM, slim_url))

    return AgentCard(
        name=f"Robot Agent - {robot_id}",
        description=(
            f"{type_name} robot agent{zone_desc}. "
            f"Physical robot controller operating under Command Center."
        ),
        url=preferred_url,
        preferred_transport=preferred_transport,
        version="4.0.0",
        defaultInputModes=["text", "application/json"],
        defaultOutputModes=["text", "application/json"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[build_agent_skill(robot_type, zone)],
        additional_interfaces=interfaces,
    )
