"""
TargetActionPlan: decide and execute the action to take when a target is detected.

Approach (physical locomotion toward the target) is intentionally NOT
supported.  The robot stays where it is and either:
  - executes the planned final_action immediately (e.g. wave / sit_down)
  - or signals MISSION_COMPLETE for find/detect/watch tasks.

Usage:
    plan_obj = TargetActionPlan(robot, brain)
    plan = await plan_obj.plan(task_goal, detected_label)  # one LLM call, cached
    await plan_obj.execute_action(plan["final_action"])
"""

import logging
from typing import Optional

from ..infra.robot_client import robot_client
from ..infra.robot_registry import robot_registry, RegisteredRobot
from .brain import LLMClient

logger = logging.getLogger(__name__)


class TargetActionPlan:
    """Plan + execute the action to take on target detection.

    No physical approach — final_action runs from the robot's current pose.
    """

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
        """Call LLM once to decide what to do on target detection.  Caches result.

        Returns: {"final_action": "wave" | "sit_down" | ... | "MISSION_COMPLETE",
                  "reason": "..."}
        """
        if self._plan is not None:
            return self._plan

        self._plan = await self.brain.decide_target_plan(
            task_goal=task_goal,
            detected_label=detected_label,
            available_actions=self.robot.actions + ["MISSION_COMPLETE"],
        )
        logger.info(
            f"[{self._rid}] Target plan: action={self._plan['final_action']}"
        )
        return self._plan

    async def execute_action(self, action: str):
        """Execute the final action on the robot from its current pose."""
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
