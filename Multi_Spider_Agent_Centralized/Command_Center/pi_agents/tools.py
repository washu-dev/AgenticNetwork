"""
Robot Agent tools: physical actions executed on the robot hardware.

Tool definitions come from each robot's /capabilities endpoint (dynamic).
Tool execution goes through robot_client HTTP POST to Pi's Thin Client.

Unlike Planner tools (static, defined here), Robot Agent tools are
discovered at runtime — each robot type may have different capabilities.
"""

from ..infra.robot_client import robot_client
from ..infra.robot_registry import RegisteredRobot


async def execute_action(robot: RegisteredRobot, action: str, params: dict = None) -> dict:
    """Execute a single physical action on a robot.

    This is the Robot Agent's equivalent of call_tool() — it dispatches
    an action name to the hardware via HTTP.

    Args:
        robot: Target robot from registry
        action: Action name (e.g. 'stand_up', 'wave', 'turn_left_angle')
        params: Optional parameters (e.g. {'steps': 3, 'angle': 20})

    Returns:
        dict with 'status', 'duration_ms', etc.
    """
    return await robot_client.execute_action(
        robot.host, robot.port, action, params, robot_id=robot.robot_id
    )


async def read_ultrasonic(robot: RegisteredRobot) -> float | None:
    """Read ultrasonic distance sensor."""
    return await robot_client.read_ultrasonic(
        robot.host, robot.port, robot_id=robot.robot_id
    )


async def speak(robot: RegisteredRobot, text: str):
    """Make the robot speak via TTS."""
    await robot_client.speak(
        robot.host, robot.port, text, robot_id=robot.robot_id
    )
