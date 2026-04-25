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

from ..config import PI_BRAIN_LLM_MODEL, LLM_MAX_TOKENS
from ..config import LLM_MODEL, create_llm, create_llm_for_model
from .schemas import ActionDecomposition, ObservationDecision, ObstacleDecision, TargetPlan, PatrolRoute, PatrolReport

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
            "ALLOWED auto-insertions (do these when needed):\n"
            "  * PREPEND stand_up BEFORE any gesture/movement if the robot might be sitting. "
            "This is a required hardware prerequisite.\n"
            "FORBIDDEN auto-insertions (never do these):\n"
            "  * Do NOT APPEND sit_down, 'reset posture', or any trailing cleanup action "
            "unless the user explicitly asked for it.\n"
            "  * Do NOT insert extra actions between the user's requested actions.\n\n"
            "Other rules:\n"
            "- For ROTATIONS, use 'turn_left_angle' or 'turn_right_angle' with "
            "params={'degrees': N}. The framework automatically converts degrees→steps "
            "using the robot's calibration. Examples:\n"
            "    'turn left 90'  → [{'action': 'turn_left_angle',  'params': {'degrees': 90}}]\n"
            "    'spin 360'      → [{'action': 'turn_left_angle',  'params': {'degrees': 360}}]\n"
            "    'turn right 45' → [{'action': 'turn_right_angle', 'params': {'degrees': 45}}]\n"
            "- Minimize rotation calls to reduce cumulative error. For 360°, prefer 2×180° "
            "over 4×90°.\n"
            "- Unless the user specifies a repeat count, execute each gesture action exactly once. "
            "Do not add extra repetitions.\n"
            "- For REPEATED gestures (e.g. 'wave 3 times', 'push_up twice'), "
            "emit ONE action entry with params={'steps': N}. "
            "Do NOT emit N copies of the same action. "
            "Example: 'wave 3 times' → [{'action': 'wave', 'params': {'steps': 3}}], "
            "NOT [{'action': 'wave'}, {'action': 'wave'}, {'action': 'wave'}].\n"
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

    logger.info(f"Creating separate brain LLM: {PI_BRAIN_LLM_MODEL}")
    return create_llm_for_model(PI_BRAIN_LLM_MODEL, temperature=0.3, max_tokens=1024)


class LLMClient:
    """Calls cloud LLM for robot decision-making via LangChain structured output."""

    def __init__(self, robot_type: str = "spider"):
        self._llm = _create_brain_llm()
        self._decompose_llm = self._llm.with_structured_output(ActionDecomposition)
        self._observe_llm = self._llm.with_structured_output(ObservationDecision)
        self._obstacle_llm = self._llm.with_structured_output(ObstacleDecision)
        self._target_plan_llm = self._llm.with_structured_output(TargetPlan)
        self._patrol_route_llm = self._llm.with_structured_output(PatrolRoute)
        self._patrol_report_llm = self._llm.with_structured_output(PatrolReport)
        self._profile = _get_profile(robot_type)
        self._robot_type = robot_type
        logger.info(f"PiAgentBrain LLMClient initialized (type={robot_type}, {PI_BRAIN_LLM_MODEL})")

    async def decompose_actions(self, prompt: str, available_actions: list, actions_description: str = "") -> list[dict]:
        """
        Parse a natural language command into an ordered list of robot actions.

        Called for task_type="direct_control" with a free-text prompt.
        Args:
            available_actions: list of valid action name strings (for validation)
            actions_description: formatted capability descriptions with params (for LLM context)
        Returns: list of dicts, e.g. [{"action": "stand_up"}, {"action": "turn_left", "params": {"degrees": 90}}]
        """
        actions_block = actions_description or ', '.join(available_actions)
        system = (
            f"{self._profile['identity']} Parse the user command into "
            "an ordered list of actions with parameters.\n\n"
            f"Available actions:\n{actions_block}\n\n"
            "Rules:\n"
            "- Only use actions from the list above\n"
            "- For turn_left / turn_right, set 'degrees' to the desired rotation amount\n"
            "- Leave steps=0 and degrees=0 for actions that don't need parameters (uses defaults)\n"
            "- Preserve EVERY action verb the user named, in the order given. If the prompt "
            "is 'forward 3 steps then turn left 90', the output MUST contain both a forward "
            "step AND a turn_left step — never silently drop the second clause.\n"
            f"{self._profile['decompose_rules']}"
        )

        try:
            result = await self._decompose_llm.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=prompt),
            ])
            valid = []
            for s in result.actions:
                if s.action not in available_actions:
                    continue
                entry = {"action": s.action}
                params = {}
                if s.steps > 0:
                    params["steps"] = s.steps
                if s.degrees > 0:
                    params["degrees"] = s.degrees
                if params:
                    entry["params"] = params
                valid.append(entry)
            if not valid:
                logger.warning(f"No valid actions from LLM decomposition: {result.actions}")
                return [{"action": "stop"}]
            reason = getattr(result, 'reason', '')
            logger.info(f"LLM decomposed '{prompt}' -> {valid} ({reason})")
            return valid, reason
        except Exception as e:
            logger.error(f"Failed to decompose actions: {e}")
            return [{"action": "stop"}], ""

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

        The robot does NOT physically approach the target.  We only decide
        what action (if any) to execute from the robot's current pose.

        Returns: {"final_action": "wave" | "MISSION_COMPLETE" | ..., "reason": "..."}
        """
        system = (
            f"{self._profile['identity']} A target has been detected by the camera system. "
            "The detection has ALREADY been verified by the upstream perception "
            "pipeline (YOLO + VLM).  The robot does NOT physically move toward "
            "the target — your job is only to decide which action to execute "
            "from the robot's CURRENT pose.\n\n"
            f"Available actions: {', '.join(available_actions)}\n\n"
            "Rules:\n"
            "- If the task names a follow-up action to perform when the target "
            "is found (e.g. 'if found, wave', 'find person and sit down'), "
            "set final_action to that action.\n"
            "- If the task is JUST to find / detect / report / watch / locate / "
            "identify the target with NO follow-up action mentioned, set "
            "final_action='MISSION_COMPLETE'.\n"
            "  Examples that map to MISSION_COMPLETE:\n"
            "    'find a tank' / 'find a person wearing red' /\n"
            "    'detect any hostiles' / 'identify the orange-shirt subject' /\n"
            "    'watch for combatants' / 'locate the casualty'.\n"
            "  Do NOT pick a 'verification' action like look_around or "
            "turn_left just because attributes were named — VLM has already\n"
            "  verified the attributes.  No physical motion is needed.\n"
            "- final_action must be one of the available actions or 'MISSION_COMPLETE'.\n"
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
            logger.info(f"Target plan: action={action} — {result.reason}")
            return {"final_action": action, "reason": result.reason}
        except Exception as e:
            logger.error(f"Target plan decision failed: {e}")
            return {"final_action": "MISSION_COMPLETE", "reason": "LLM error"}

    async def plan_patrol_route(
        self, task_goal: str, map_data: dict = None,
        route_prompt: str = "",
    ) -> dict:
        """
        Generate a patrol route based on the user's task goal.

        NOTE: "area" (LLM-inferred walking route) is currently DISABLED.
        With no SLAM / localization the LLM has no map to reason about, so
        we fold an LLM-classified "area" request into "spin" — the robot
        rotates in place to convey activity and detection still fires.

        EXCEPTION: when the user EXPLICITLY dictates a walking route in the
        prompt (e.g. 'forward 5 steps, turn left 90, forward 5'), the
        Planner extracts it into `route_prompt`.  We bypass the classifier
        and return pattern='custom' with the prompt forwarded — the Robot
        Agent expands it into action steps via execute_steps_from_prompt.
        This works on a real robot; on the mock_pi the locomotion commands
        sleep + ACK without changing the recorded video.

        Called once at mission start.
        Returns: {"pattern": "spin"|"stationary"|"custom",
                  "steps": [...]   (empty for custom — robot agent expands),
                  "route_prompt": str   (only for custom),
                  "reason": "..."}
        """
        # Short-circuit: user-dictated route → custom pattern, no LLM needed.
        if route_prompt:
            return {
                "pattern": "custom",
                "steps": [],
                "route_prompt": route_prompt,
                "reason": f"Following user-dictated route: {route_prompt}",
            }

        # Step 1: LLM decides which pattern to use AND explains why
        pattern, llm_reason = await self._classify_patrol_pattern(task_goal)

        # Step 2: Generate steps based on pattern. The LLM's rationale is
        # preserved verbatim so the reasoning trace reflects the actual
        # decision instead of a boilerplate string.
        if pattern == "stationary":
            return {
                "pattern": "stationary",
                "steps": [],
                "reason": llm_reason or "Stationary observation",
            }

        # "area" is currently folded into "spin" — the classifier may still
        # return "area" if the user said 'patrol'/'search'/'explore', but we
        # treat it as spin so the robot doesn't try to physically walk.
        if pattern in ("spin", "area"):
            note = ""
            if pattern == "area":
                note = " (area→spin: locomotion disabled in this build)"
            return {
                "pattern": "spin",
                "steps": [
                    {"action": "turn_left", "value": 90},
                    {"action": "turn_left", "value": 90},
                    {"action": "turn_left", "value": 90},
                    {"action": "turn_left", "value": 90},
                ],
                "reason": (llm_reason or "360° scan in place") + note,
            }

        # --- Disabled: area patrol (forward + turn route) ---
        # if map_data:
        #     # TODO: Use LLM + map_data to plan an informed route
        #     logger.info("Map data provided — LLM-based route planning (not yet implemented)")
        #
        # # Default fixed route: forward 10, turn left 90°, forward 5
        # return {
        #     "pattern": "area",
        #     "steps": [
        #         {"action": "forward", "value": 10},
        #         {"action": "turn_left", "value": 90},
        #         {"action": "forward", "value": 5},
        #     ],
        #     "reason": llm_reason or "Default patrol route (no map available)",
        # }

        # Unreachable in practice (pattern is one of the three above), but
        # keep a safe default just in case.
        return {
            "pattern": "stationary",
            "steps": [],
            "reason": llm_reason or "Defaulted to stationary (unknown pattern)",
        }

    async def _classify_patrol_pattern(self, task_goal: str) -> tuple[str, str]:
        """Use LLM to classify task_goal into (pattern, reason).

        Patterns: 'stationary' (default) or 'spin' (in-place 360° rotation).
        Custom-route patrols are picked up by plan_patrol_route's
        short-circuit before this function is even called.

        DEFAULT BEHAVIOUR IS STATIONARY.  Only escalate to spin when the
        user EXPLICITLY asks the robot to rotate / look around / scan.
        Find/search/patrol intent ALONE is NOT enough — without an
        explicit rotation cue, the robot observes from its current pose.
        Empty goal → stationary without consulting the LLM (avoids a
        wasted call when the prompt has no observation intent).
        """
        if not task_goal:
            return "stationary", "Empty goal — defaulted to stationary"

        system = (
            "Classify the mission goal into ONE of two patrol patterns AND explain why.\n\n"
            "Patterns:\n"
            "- 'stationary' (DEFAULT): the robot stays in its current pose and\n"
            "  observes from there.  Use this for: 'check here', 'report what you\n"
            "  see', 'describe', 'detect', 'watch / monitor / alert on X',\n"
            "  'count', 'find X', 'search for X', 'patrol' (the verb alone),\n"
            "  'SITREP', and ANY prompt that does NOT explicitly tell the robot\n"
            "  to rotate or look around.\n"
            "- 'spin': rotate 360° in place, sampling each ~90° quadrant.  Use\n"
            "  ONLY when the prompt EXPLICITLY tells the robot to perform a\n"
            "  rotation or look-around motion.  Example trigger phrases:\n"
            "    'spin', 'spin around', 'turn around', 'look around',\n"
            "    'scan around', 'scan the area', 'scan the sector',\n"
            "    'scan the perimeter', 'sweep', 'sweep the sector',\n"
            "    '360', '360 degrees', 'rotate', 'rotate in place',\n"
            "    'all directions', 'survey the area', 'survey the surroundings',\n"
            "    'check all directions', 'visual sweep', 'visual scan'.\n"
            "  Military 'scan' verbs targeting a region (perimeter / sector /\n"
            "  area / surroundings) ALL imply rotational coverage and count\n"
            "  as spin triggers.  Synonyms and paraphrases also count\n"
            "  (e.g. 'do a slow spin clockwise', 'scan back-and-forth').\n\n"
            "DEFAULT IS STATIONARY.  When in doubt, choose stationary — the\n"
            "user can always re-issue the command with an explicit rotation\n"
            "cue, but a wrongly-triggered spin wastes ~10 s and may move the\n"
            "robot's camera away from the relevant subject.\n\n"
            "Fill the structured output:\n"
            "- pattern: 'stationary' or 'spin'\n"
            "- steps: [] (the caller supplies concrete steps)\n"
            "- reason: one short sentence linking the user's words to the chosen pattern.\n\n"
            "DO NOT emit 'area' — it's disabled in this build."
        )
        try:
            result = await self._patrol_route_llm.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=f"Mission goal: {task_goal}"),
            ])
            pattern = result.pattern.strip().lower()
            if pattern == "area":
                pattern = "spin"
            if pattern not in ("stationary", "spin"):
                pattern = "stationary"
            reason = (result.reason or "").strip()
            logger.info(f"Patrol pattern (LLM): {pattern} — {reason} (goal: {task_goal[:50]})")
            return pattern, reason
        except Exception as e:
            logger.warning(f"Pattern classification failed: {e}, defaulting to stationary")
            return "stationary", "Pattern classification failed — defaulted to stationary"

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
            "- DEDUPLICATE: The same object seen across consecutive frames is ONE object, not many. "
            "Use time and position to judge: detections close in time at similar positions are the same object.\n"
            "- Set target_count to the number of DISTINCT targets (not total detections).\n"
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
                "target_count": result.target_count,
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
