"""
TargetApproach: independent module for approaching a detected target and executing an action.

This module is designed to be portable:
- Currently used by spider (PiCrawler) with bbox-based navigation
- Will be migrated to turtlebot with nav2-based navigation
- plan() and execute_action() are reusable across robot types
- approach_step() is robot-type-specific (override for different robots)

Usage:
    approach = SpiderTargetApproach(robot, brain, robot_client)
    plan = await approach.plan(task_goal, detected_label)  # LLM once
    if plan["approach"]:
        action = approach.approach_step(detections, ultrasonic_cm)  # rules, per frame
        if action == plan["final_action"]:
            await approach.execute_action(action)  # done
"""

import logging
from typing import Optional

from ..infra.robot_client import robot_client
from ..infra.robot_registry import robot_registry, RegisteredRobot
from .brain import LLMClient

logger = logging.getLogger(__name__)

# Frame dimensions (must match detection engine)
FRAME_WIDTH = 640
FRAME_HEIGHT = 480

# Close enough thresholds by action category
_CLOSE_THRESHOLDS = {
    "wave": 50, "dance": 50, "nod": 50, "shake_head": 50,
    "shake_hand": 20, "play_dead": 30,
}
_DEFAULT_CLOSE_THRESHOLD = 30


def _get_close_threshold(action: str) -> float:
    return _CLOSE_THRESHOLDS.get(action, _DEFAULT_CLOSE_THRESHOLD)


def _get_bbox_center_x(bbox) -> Optional[float]:
    if isinstance(bbox, dict):
        x1, x2 = bbox.get("x1", 0), bbox.get("x2", 0)
    elif isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        x1, x2 = bbox[0], bbox[2]
    else:
        return None
    return (x1 + x2) / 2


def _get_bbox_height(bbox) -> Optional[float]:
    if isinstance(bbox, dict):
        y1, y2 = bbox.get("y1", 0), bbox.get("y2", 0)
    elif isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        y1, y2 = bbox[1], bbox[3]
    else:
        return None
    return y2 - y1


def _estimate_turn_degrees(bbox) -> float:
    """Estimate how many degrees to turn based on target bbox offset from center."""
    cx = _get_bbox_center_x(bbox)
    if cx is None:
        return 15.0
    camera_h_fov = 62.0
    pixel_offset = abs(cx - FRAME_WIDTH / 2)
    degrees = pixel_offset / (FRAME_WIDTH / 2) * (camera_h_fov / 2)
    return round(max(degrees, 5.0), 1)


class TargetApproach:
    """Base class for target approach logic. Robot-type-specific subclasses override approach_step()."""

    def __init__(self, robot: RegisteredRobot, brain: LLMClient):
        self.robot = robot
        self.brain = brain
        self._plan: Optional[dict] = None  # cached plan from LLM
        self._rid = robot.robot_id

    @property
    def has_plan(self) -> bool:
        return self._plan is not None

    @property
    def cached_plan(self) -> Optional[dict]:
        return self._plan

    def clear(self):
        """Reset plan cache (new mission)."""
        self._plan = None

    async def plan(self, task_goal: str, detected_label: str) -> dict:
        """Call LLM once to decide approach strategy. Caches result.

        Returns: {"approach": bool, "final_action": "wave", "reason": "..."}
        """
        if self._plan is not None:
            return self._plan

        self._plan = await self.brain.decide_target_plan(
            task_goal=task_goal,
            detected_label=detected_label,
            available_actions=self.robot.actions + ["MISSION_COMPLETE"],
        )
        logger.info(
            f"[{self._rid}] Target plan: approach={self._plan['approach']}, "
            f"action={self._plan['final_action']}"
        )
        return self._plan

    def approach_step(self, detections: list, ultrasonic_cm: Optional[float]) -> str:
        """Decide one navigation step based on sensor data. Override per robot type.

        Returns: action name ("turn_left", "forward", or the final_action when close enough)
        """
        raise NotImplementedError("Subclasses must implement approach_step()")

    async def execute_action(self, action: str):
        """Execute the final action on the robot."""
        if action == "MISSION_COMPLETE":
            logger.info(f"[{self._rid}] Mission complete (no physical action)")
            return
        result = await robot_client.execute_action(
            self.robot.host, self.robot.port, action, robot_id=self._rid
        )
        if action == "stand_up":
            robot_registry.update_standing(self._rid, True)
        elif action in ("sit_down", "stop"):
            robot_registry.update_standing(self._rid, False)
        logger.info(f"[{self._rid}] Executed final action: {action}")
        return result


class SpiderTargetApproach(TargetApproach):
    """Spider (PiCrawler) approach: uses bbox position + ultrasonic distance."""

    def approach_step(self, detections: list, ultrasonic_cm: Optional[float]) -> str:
        """Bbox-based navigation for spider robot.

        Returns:
            - "turn_left" / "turn_right": target is off-center
            - "forward": target is centered but far
            - final_action: target is close enough → execute
            - "stop": no target in frame (lost target)
        """
        if not self._plan:
            return "stop"

        # Find target detection
        target_dets = [d for d in detections if d.get("is_target")]
        if not target_dets:
            return "forward"  # target lost, keep moving forward

        target = target_dets[0]
        bbox = target.get("bbox", {})
        cx = _get_bbox_center_x(bbox)
        bbox_h = _get_bbox_height(bbox)

        # Check if close enough
        threshold = _get_close_threshold(self._plan["final_action"])
        is_close_ultrasonic = ultrasonic_cm is not None and ultrasonic_cm <= threshold
        is_close_bbox = bbox_h is not None and bbox_h > FRAME_HEIGHT * 0.5

        if is_close_ultrasonic or is_close_bbox:
            return self._plan["final_action"]

        # Navigate toward target
        if cx is None:
            return "forward"

        if cx < FRAME_WIDTH * 0.35:
            return "turn_left"
        elif cx > FRAME_WIDTH * 0.65:
            return "turn_right"
        else:
            return "forward"

    def get_turn_degrees(self, detections: list) -> float:
        """Calculate precise turn angle from bbox."""
        target_dets = [d for d in detections if d.get("is_target")]
        if not target_dets:
            return 15.0
        return _estimate_turn_degrees(target_dets[0].get("bbox", {}))
