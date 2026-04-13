"""
PiCrawler capabilities: single source of truth.

Every part of the system reads from here:
- thin_client.py: maps action names to hardware methods
- card.py: tells ADS what skills we have
- Central: reads /capabilities to know what actions Pi supports
"""

from dataclasses import dataclass
from typing import List


@dataclass
class Action:
    """A single action the robot can perform."""
    name: str
    description: str
    category: str  # "movement", "posture", "gesture", "look"
    requires_standing: bool = True
    parameters: str = ""  # human-readable parameter info


# ── All actions the robot supports ──────────────────────────

ACTIONS: List[Action] = [
    # Posture
    Action(
        name="stand_up",
        description="Stand up from sitting position. Required before any movement or gesture.",
        category="posture",
        requires_standing=False,
    ),
    Action(
        name="sit_down",
        description="Sit down. Safe resting position, reduces servo strain.",
        category="posture",
        requires_standing=False,
    ),
    Action(
        name="stop",
        description="Stop all movement and sit down.",
        category="posture",
        requires_standing=False,
    ),

    # Movement
    Action(
        name="forward",
        description="Walk forward. Each step moves ~9cm. Set steps=N to walk N steps.",
        category="movement",
        parameters="steps: number of steps (default 1, each ~9cm)",
    ),
    Action(
        name="backward",
        description="Walk backward. Each step moves ~9cm. Set steps=N to walk N steps.",
        category="movement",
        parameters="steps: number of steps (default 1, each ~9cm)",
    ),
    Action(
        name="turn_left",
        description="Turn left in place. Specify degrees (e.g. degrees=90 for a quarter turn, degrees=180 for a half turn).",
        category="movement",
        parameters="degrees: rotation amount in degrees",
    ),
    Action(
        name="turn_right",
        description="Turn right in place. Specify degrees (e.g. degrees=90 for a quarter turn, degrees=180 for a half turn).",
        category="movement",
        parameters="degrees: rotation amount in degrees",
    ),

    # Gestures
    Action(
        name="wave",
        description="Raise front-left leg and wave. Use as a greeting when you see a person.",
        category="gesture",
    ),
    Action(
        name="dance",
        description="Perform a dance routine with body movements and rotation.",
        category="gesture",
    ),
    Action(
        name="push_up",
        description="Do push-ups (lower and raise the body).",
        category="gesture",
    ),
    Action(
        name="nod",
        description="Nod head up and down. Use to signal agreement or acknowledgement.",
        category="gesture",
    ),
    Action(
        name="shake_head",
        description="Shake head side to side. Use to signal disagreement or 'no'.",
        category="gesture",
    ),
    Action(
        name="shake_hand",
        description="Extend front leg for a handshake gesture.",
        category="gesture",
    ),
    Action(
        name="play_dead",
        description="Flip over and play dead. A fun trick.",
        category="gesture",
        requires_standing=False,
    ),

    # Look (head/body tilt, does NOT move position)
    Action(
        name="look_left",
        description="Tilt body to look left. Does NOT turn — use turn_left(degrees=...) to change facing direction.",
        category="look",
    ),
    Action(
        name="look_right",
        description="Tilt body to look right. Does NOT turn — use turn_right(degrees=...) to change facing direction.",
        category="look",
    ),
    Action(
        name="look_up",
        description="Tilt body to look upward.",
        category="look",
    ),
    Action(
        name="look_down",
        description="Tilt body to look downward.",
        category="look",
    ),
]

# Just the names (for quick access)
ACTION_NAMES = [a.name for a in ACTIONS]


# ── Sensors and other capabilities ──────────────────────────

# ── Turn calibration ────────────────────────────────────────
# Per-robot calibration for _turn_by_degrees on the Command Center.
# deg_per_step: actual degrees turned per hardware step at the given angle_param.
# angle_param:  leg swing parameter passed to turn_left_angle / turn_right_angle.
TURN_CALIBRATION = {
    "deg_per_step": 30,
    "angle_param": 25,
}

SENSORS = [
    {"name": "ultrasonic", "description": "Distance sensor (2-400cm), used for obstacle avoidance", "pins": "D2/D3"},
    {"name": "camera", "description": "Raspberry Pi camera, MJPEG stream on port 9000", "resolution": "640x480"},
]

ABILITIES = [
    {"name": "obstacle_avoidance", "description": "Reactive ultrasonic obstacle avoidance during walking"},
    {"name": "tts", "description": "Text-to-speech via local TTS engine (configurable, default off)"},
    {"name": "sound_effects", "description": "Play sound effects: mission_start, detection, mission_complete, error, obstacle, alert"},
]

ENDPOINTS = {
    "capabilities": "/capabilities",
    "health": "/health",
    "status": "/status",
    "video_stream": "http://localhost:9000/mjpg",
}


# ── Helper functions ────────────────────────────────────────

def get_actions_description() -> str:
    """Format all actions as a string for LLM prompts."""
    lines = []
    for a in ACTIONS:
        line = f"- {a.name}: {a.description}"
        if a.requires_standing:
            line += " (requires standing)"
        lines.append(line)
    lines.append("- MISSION_COMPLETE: Signal that the task goal has been fully accomplished")
    return "\n".join(lines)


def get_capabilities_json() -> dict:
    """Full capabilities payload for GET /capabilities endpoint."""
    return {
        "actions": [
            {
                "name": a.name,
                "description": a.description,
                "category": a.category,
                "requires_standing": a.requires_standing,
            }
            for a in ACTIONS
        ],
        "sensors": SENSORS,
        "abilities": ABILITIES,
        "endpoints": ENDPOINTS,
        "turn_calibration": TURN_CALIBRATION,
    }
