"""
Pi Agent Brain: LLM-powered decision engine for robots.

Uses LangChain with structured output (.with_structured_output()) for
reliable, typed JSON responses. Shares the LLM factory with the planner.

Model is configured via PI_BRAIN_LLM_MODEL (defaults to LLM_MODEL).

Prompt templates are keyed by robot_type (spider, turtlebot, car).
New robot types only need a new entry in ROBOT_PROFILES.
"""

import logging

from langchain_core.messages import SystemMessage, HumanMessage

from ..config import PI_BRAIN_LLM_MODEL
from ..planner.llm_config import LLM_MODEL, create_llm
from .schemas import ActionDecomposition, ObservationDecision, ObstacleDecision, TargetPlan, PatrolReport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Robot type profiles — one entry per robot type
# ---------------------------------------------------------------------------

ROBOT_PROFILES = {
    "spider": {
        "identity": (
            "You are a PiCrawler robot agent. You are a spider-like quadruped robot "
            "with 4 legs and a camera."
        ),
        "decompose_rules": (
            "- The robot must stand_up before it can move or perform gestures. "
            "If the command implies movement or gestures and doesn't start with stand_up, "
            "prepend stand_up automatically.\n"
        ),
        "observation_rules": (
            "- You must stand_up before you can move or perform gestures\n"
            "- If a [TARGET] is on the left, turn_left to face it\n"
            "- If a [TARGET] is on the right, turn_right to face it\n"
            "- If a [TARGET] is at center and far, move forward to approach\n"
            "- If a [TARGET] is at center and close (or ultrasonic <= 30cm), "
            "execute the response action specified in the task goal (e.g. wave, dance, sit_down)\n"
            "- Ultrasonic sensor measures distance to nearest object ahead (in cm). "
            "Use it together with bbox size to judge distance. <=30cm = close, >30cm = far\n"
            "- look_left/look_right only tilts the body, does NOT change facing direction\n"
            "- turn_left/turn_right changes facing direction (~90 degrees)\n"
        ),
        "obstacle_rules": (
            "An obstacle has been detected ahead by the ultrasonic sensor.\n\n"
            "You must choose ONE avoidance action:\n"
            "- backward: back up a few steps\n"
            "- turn_left: turn left to go around the obstacle\n"
            "- turn_right: turn right to go around the obstacle\n"
            "- stop: stop and wait\n\n"
            "Consider the distance. If very close (<10cm), back up first. "
            "Otherwise, turn to go around it."
        ),
    },
    "turtlebot": {
        "identity": (
            "You are a TurtleBot robot agent. You are a wheeled robot with lidar, "
            "a camera, and map-based localization. You can navigate to precise coordinates."
        ),
        "decompose_rules": (
            "- The robot uses wheels for movement (no stand_up needed).\n"
            "- Navigation commands use map coordinates when available.\n"
        ),
        "observation_rules": (
            "- You have lidar for precise distance measurement and obstacle detection\n"
            "- You can localize targets on a map with coordinates\n"
            "- If a [TARGET] is detected, use lidar + camera to determine precise position\n"
            "- Navigation uses map-based path planning, not simple turns\n"
        ),
        "obstacle_rules": (
            "An obstacle has been detected by the lidar sensor.\n\n"
            "You must choose ONE avoidance action:\n"
            "- navigate_around: use path planner to go around the obstacle\n"
            "- backward: reverse away from the obstacle\n"
            "- stop: stop and wait\n\n"
            "Use lidar data to choose the best path."
        ),
    },
    "car": {
        "identity": (
            "You are a Car robot agent. You are a wheeled vehicle robot "
            "designed for task execution at specific locations."
        ),
        "decompose_rules": (
            "- The robot uses wheels for movement (no stand_up needed).\n"
            "- Navigate to target locations before executing tasks.\n"
        ),
        "observation_rules": (
            "- You navigate to specified coordinates to execute tasks\n"
            "- Use camera to verify you are at the correct location\n"
            "- Execute the assigned task when at the target position\n"
        ),
        "obstacle_rules": (
            "An obstacle has been detected ahead.\n\n"
            "You must choose ONE avoidance action:\n"
            "- navigate_around: reroute to avoid obstacle\n"
            "- backward: reverse away\n"
            "- stop: stop and wait\n\n"
            "Choose based on mission urgency and obstacle size."
        ),
    },
}


def _get_profile(robot_type: str) -> dict:
    """Get profile for robot_type, default to spider."""
    return ROBOT_PROFILES.get(robot_type, ROBOT_PROFILES["spider"])


def _create_brain_llm():
    """Create LLM instance for pi agent brain.

    Reuses the planner's cached LLM if the model is the same.
    Otherwise creates a dedicated instance for the brain model.
    """
    if PI_BRAIN_LLM_MODEL == LLM_MODEL:
        return create_llm()

    # Different model — create a separate instance
    if "/" in PI_BRAIN_LLM_MODEL:
        provider, model_name = PI_BRAIN_LLM_MODEL.split("/", 1)
    else:
        provider, model_name = "openai", PI_BRAIN_LLM_MODEL

    provider = provider.lower()
    logger.info(f"Creating separate brain LLM: {PI_BRAIN_LLM_MODEL}")

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model_name, temperature=0.3, max_tokens=1024)
    else:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model_name, temperature=0.3, max_tokens=1024)


class LLMClient:
    """Calls cloud LLM for robot decision-making via LangChain structured output."""

    def __init__(self, robot_type: str = "spider"):
        self._llm = _create_brain_llm()
        self._decompose_llm = self._llm.with_structured_output(ActionDecomposition)
        self._observe_llm = self._llm.with_structured_output(ObservationDecision)
        self._obstacle_llm = self._llm.with_structured_output(ObstacleDecision)
        self._target_plan_llm = self._llm.with_structured_output(TargetPlan)
        self._patrol_report_llm = self._llm.with_structured_output(PatrolReport)
        self._profile = _get_profile(robot_type)
        self._robot_type = robot_type
        logger.info(f"PiAgentBrain LLMClient initialized (type={robot_type}, {PI_BRAIN_LLM_MODEL})")

    async def decompose_actions(self, prompt: str, available_actions: list) -> list:
        """
        Parse a natural language command into an ordered list of robot actions.

        Called for task_type="direct_control" with a free-text prompt.
        Returns: list of action name strings, e.g. ["stand_up", "wave", "dance"]
        """
        system = (
            f"{self._profile['identity']} Parse the user command into "
            "an ordered list of actions. Available actions:\n"
            f"{', '.join(available_actions)}\n\n"
            "Rules:\n"
            "- Only use actions from the list above\n"
            f"{self._profile['decompose_rules']}"
        )

        try:
            result = await self._decompose_llm.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=prompt),
            ])
            valid = [a for a in result.actions if a in available_actions]
            if not valid:
                logger.warning(f"No valid actions from LLM decomposition: {result.actions}")
                return ["stop"]
            logger.info(f"LLM decomposed '{prompt}' -> {valid}")
            return valid
        except Exception as e:
            logger.error(f"Failed to decompose actions: {e}")
            return ["stop"]

    async def decide_observation_response(
        self,
        task_goal: str,
        observations: str,
        elapsed_s: float,
        available_actions: list,
        actions_description: str = "",
        robot_state: str = "",
    ) -> dict:
        """
        Decide what action to take based on detection observations.

        This is the core agent decision: Central detects, this LLM decides,
        then the command is sent to the robot for execution.

        Returns: {"action": "...", "reason": "..."}
        """
        system = (
            f"{self._profile['identity']} You receive observations from a camera system "
            "and must decide your next action to accomplish your mission.\n\n"
            "Your available actions:\n"
            f"{actions_description}\n\n"
            "Rules:\n"
            "- Choose exactly ONE action from the list above\n"
            f"{self._profile['observation_rules']}"
            "- IMPORTANT: There are two types of tasks:\n"
            "  1) REPORT tasks ('report', 'detect', 'observe', 'identify', 'describe'): "
            "Do NOT output MISSION_COMPLETE. Just output 'stop' and let the patrol continue. "
            "Reporting is automatic - the system records all detections along the route.\n"
            "  2) ACTION tasks ('if found X, wave/dance/sit_down', etc.): "
            "When the [TARGET] is found and close, execute the specified action, "
            "then output MISSION_COMPLETE to stop the patrol.\n"
            "- Only perform physical actions (wave, dance, sit_down, etc.) if the task goal "
            "explicitly asks for them.\n"
            "- If you are already standing (standing=True), do NOT choose stand_up again\n"
            "- If no targets detected, continue patrol (do nothing - return stop)\n"
        )
        user = (
            f"Mission goal: {task_goal}\n"
            f"Current state: {robot_state}\n"
            f"Time elapsed: {elapsed_s:.0f}s\n\n"
            f"Observations:\n{observations}\n\n"
            f"Available actions: {', '.join(available_actions)}"
        )

        try:
            result = await self._observe_llm.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=user),
            ])
            action = result.action
            if action not in available_actions:
                logger.warning(f"LLM chose invalid action '{action}', defaulting to stop")
                action = "stop"
            return {"action": action, "reason": result.reason}
        except Exception as e:
            logger.error(f"Observation decision failed: {e}")
            return {"action": "stop", "reason": "LLM error"}

    async def decide_obstacle_response(self, distance_cm: float) -> dict:
        """
        Decide how to handle an obstacle during patrol.

        Returns: {"action": "...", "reason": "..."}
        Action is one of: backward, turn_left, turn_right, stop
        """
        system = (
            f"{self._profile['identity']} You are patrolling an area. "
            f"{self._profile['obstacle_rules']}"
        )
        user = f"Obstacle detected at {distance_cm:.0f}cm ahead. What should I do?"

        try:
            result = await self._obstacle_llm.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=user),
            ])
            return {"action": result.action, "reason": result.reason}
        except Exception as e:
            logger.error(f"Obstacle decision failed: {e}")
            return {"action": "turn_left", "reason": "LLM error, default avoidance"}

    async def decide_target_plan(
        self,
        task_goal: str,
        detected_label: str,
        available_actions: list,
    ) -> dict:
        """
        Decide what to do when a target is first detected (called once per target).

        Understands the task_goal to determine:
        - Should the robot approach the target?
        - What action to execute (when close enough, or immediately)?

        Returns: {"approach": bool, "final_action": "wave", "reason": "..."}
        """
        system = (
            f"{self._profile['identity']} A target has been detected by the camera system. "
            "Based on the mission goal, decide the plan.\n\n"
            f"Available actions: {', '.join(available_actions)}\n\n"
            "Rules:\n"
            "- If the task says to do something when the target is found (e.g. 'if found, wave'), "
            "set approach=true and final_action to that action\n"
            "- If the task is just to detect/report, set approach=false and final_action='MISSION_COMPLETE'\n"
            "- final_action must be one of the available actions or 'MISSION_COMPLETE'\n"
        )
        user = (
            f"Mission goal: {task_goal}\n"
            f"Detected: {detected_label}\n\n"
            "What is the plan?"
        )

        try:
            result = await self._target_plan_llm.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=user),
            ])
            action = result.final_action
            if action not in available_actions and action != "MISSION_COMPLETE":
                logger.warning(f"LLM chose invalid final_action '{action}', defaulting to MISSION_COMPLETE")
                action = "MISSION_COMPLETE"
            logger.info(f"Target plan: approach={result.approach}, action={action} — {result.reason}")
            return {"approach": result.approach, "final_action": action, "reason": result.reason}
        except Exception as e:
            logger.error(f"Target plan decision failed: {e}")
            return {"approach": False, "final_action": "MISSION_COMPLETE", "reason": "LLM error"}

    async def generate_patrol_report(self, task_goal: str, detections_log: list) -> dict:
        """
        Generate a structured patrol report after patrol completes.

        Args:
            task_goal: The original mission goal
            detections_log: List of all detections recorded during patrol
                [{label, confidence, time_s, position}, ...]

        Returns: {"summary": "...", "targets_found": [...], "anomalies": [...], "recommendation": "..."}
        """
        if not detections_log:
            return {
                "summary": "Patrol completed. No objects detected.",
                "targets_found": [],
                "anomalies": [],
                "recommendation": "",
            }

        det_lines = []
        for d in detections_log[:50]:  # limit to 50 for LLM context
            det_lines.append(
                f"- {d.get('label', '?')} (confidence: {d.get('confidence', 0):.0%}, "
                f"at {d.get('time_s', 0):.0f}s, position: {d.get('position', '?')})"
            )
        det_text = "\n".join(det_lines)

        system = (
            f"{self._profile['identity']} You have just completed a patrol mission. "
            "Summarize what was observed in a concise report.\n\n"
            "Rules:\n"
            "- Group duplicate detections (same object seen multiple times)\n"
            "- Note anything unusual or potentially important\n"
            "- Keep the summary brief (2-3 sentences)\n"
        )
        user = (
            f"Mission goal: {task_goal}\n"
            f"Total detections: {len(detections_log)}\n\n"
            f"Detection log:\n{det_text}"
        )

        try:
            result = await self._patrol_report_llm.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=user),
            ])
            report = {
                "summary": result.summary,
                "targets_found": result.targets_found,
                "anomalies": result.anomalies,
                "recommendation": result.recommendation,
            }
            logger.info(f"Patrol report: {result.summary[:100]}")
            return report
        except Exception as e:
            logger.error(f"Patrol report generation failed: {e}")
            labels = list({d.get("label", "?") for d in detections_log})
            return {
                "summary": f"Patrol completed. Detected: {', '.join(labels)}",
                "targets_found": labels,
                "anomalies": [],
                "recommendation": "",
            }
