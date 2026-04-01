"""
LangGraph-based Pi Agent running on Central Command Center.

Two graphs (same as the original Multi_Spider_Agent/graph.py):
1. MissionGraph: perceive → decide → act → evaluate (Agent mode)
2. Direct control: sequential step execution (Execute mode)

Key difference from Pi-side graph.py:
- Hardware calls go via robot_client (A2A) instead of local CrawlerControl
- LLM calls are async (httpx) instead of sync (requests)
- Robot state comes from robot_registry instead of control._standing

Each detection from detection_engine triggers one MissionGraph invocation.
State persists across invocations via the CentralPiAgent (replaces MissionManager).
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from .brain import LLMClient
from ..infra.robot_client import robot_client
from ..infra.robot_registry import robot_registry, RegisteredRobot

logger = logging.getLogger(__name__)

# ── Constants (from graph.py) ─────────────────────────────
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
CAMERA_H_FOV = 62.0

# Fallback action categories (used when robot has no capabilities metadata)
_DEFAULT_SETUP = {"stand_up", "sit_down", "stop"}
_DEFAULT_NAVIGATION = {
    "forward", "backward", "turn_left", "turn_right",
    "turn_left_angle", "turn_right_angle",
    "look_left", "look_right", "look_up", "look_down",
}
_DEFAULT_GESTURE = {
    "wave", "dance", "push_up", "nod", "shake_head",
    "shake_hand", "play_dead",
}

# Category → fallback set mapping
_CATEGORY_DEFAULTS = {
    "posture": _DEFAULT_SETUP,
    "movement": _DEFAULT_NAVIGATION,
    "look": _DEFAULT_NAVIGATION,
    "gesture": _DEFAULT_GESTURE,
}


def _actions_by_category(robot: "RegisteredRobot", *categories: str) -> set:
    """Get action names from a robot's capabilities by category.

    Falls back to hardcoded defaults if the robot has no capabilities metadata.
    """
    caps = robot.capabilities.get("actions", []) if robot.capabilities else []
    if not caps:
        result = set()
        for cat in categories:
            result |= _CATEGORY_DEFAULTS.get(cat, set())
        return result
    return {a["name"] for a in caps if a.get("category") in categories}


# ── State types ────────────────────────────────────────────

class MissionState(TypedDict):
    """State for the mission graph. Passed through all nodes."""
    # Mission context
    task_goal: str
    mission_id: str
    robot_id: str
    # Observation input
    obs_status: str            # "searching", "finished", "init"
    obs_reason: str            # reason for finished (timeout, cancelled)
    detections: List[Dict]
    has_target: bool
    elapsed_s: float
    # Robot state
    standing: bool
    available_actions: List[str]
    actions_description: str
    # Decision output
    obs_description: str
    action: str
    reason: str
    # Result
    outcome: str               # "acted", "setup", "completed", "ignored", "error"


# ── Helper functions (from graph.py, unchanged) ───────────

def _describe_bbox(bbox) -> str:
    if not bbox:
        return "unknown position"
    if isinstance(bbox, dict):
        x1, y1 = bbox.get("x1", 0), bbox.get("y1", 0)
        x2, y2 = bbox.get("x2", 0), bbox.get("y2", 0)
    elif isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        x1, y1, x2, y2 = bbox[0], bbox[1], bbox[2], bbox[3]
    else:
        return "unknown position"
    x_center = (x1 + x2) / 2
    box_height = y2 - y1
    h_pos = "left side" if x_center < FRAME_WIDTH * 0.25 else (
        "right side" if x_center > FRAME_WIDTH * 0.75 else "center")
    distance = "very close" if box_height > FRAME_HEIGHT * 0.5 else (
        "medium distance" if box_height > FRAME_HEIGHT * 0.15 else "far away")
    return f"{h_pos}, {distance}"


def _describe_detections(detections: list) -> str:
    if not detections:
        return "No objects detected."
    parts = []
    for det in detections:
        label = det.get("label", "unknown")
        conf = float(det.get("confidence", 0) or 0)
        position = _describe_bbox(det.get("bbox", []))
        desc = f"- {label} (confidence: {conf:.0%}, position: {position})"
        if det.get("is_target"):
            desc += " [TARGET]"
        parts.append(desc)
    return "\n".join(parts)


def _get_bbox_center_x(bbox) -> Optional[float]:
    if isinstance(bbox, dict):
        x1, x2 = bbox.get("x1", 0), bbox.get("x2", 0)
    elif isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        x1, x2 = bbox[0], bbox[2]
    else:
        return None
    return (x1 + x2) / 2


def _estimate_turn_degrees(bbox) -> float:
    cx = _get_bbox_center_x(bbox)
    if cx is None:
        return 15.0
    pixel_offset = abs(cx - FRAME_WIDTH / 2)
    degrees = pixel_offset / (FRAME_WIDTH / 2) * (CAMERA_H_FOV / 2)
    return round(max(degrees, 5.0), 1)


# ── Patrol route ──────────────────────────────────────────

DEFAULT_PATROL_ROUTE = [
    ("forward", 5),
    ("turn_left", 90),
    ("forward", 2),
    ("turn_right", 90),
    ("forward", 2),
]


# ── Mission Graph (Agent mode) ────────────────────────────

class MissionGraph:
    """
    LangGraph-based mission processor.

    Each observation from detection_engine triggers one graph invocation:
    START → perceive → decide → act → evaluate → END

    Same structure as the original Pi-side MissionGraph, but:
    - act node sends commands via robot_client (A2A) instead of local hardware
    - decide node uses async LLM calls
    """

    def __init__(self, robot: RegisteredRobot, brain: LLMClient):
        self.robot = robot
        self.brain = brain
        self._rid = robot.robot_id
        self.graph = self._build()

    def _build(self):
        g = StateGraph(MissionState)
        g.add_node("perceive", self._perceive)
        g.add_node("decide", self._decide)
        g.add_node("act", self._act)
        g.add_node("evaluate", self._evaluate)

        g.add_edge(START, "perceive")
        g.add_edge("perceive", "decide")
        g.add_edge("decide", "act")
        g.add_edge("act", "evaluate")
        g.add_edge("evaluate", END)

        return g.compile()

    def _perceive(self, state: MissionState) -> dict:
        """Parse observation into natural language description."""
        obs_status = state["obs_status"]
        detections = state.get("detections", [])
        has_target = state.get("has_target", False)
        reason_finished = state.get("obs_reason", "")

        if obs_status == "init":
            obs_desc = (
                "Mission just started. No observations yet. "
                "Decide what initial setup actions are needed "
                "(e.g. stand_up to prepare for detection)."
            )
        elif obs_status == "finished":
            if has_target:
                obs_desc = f"Mission ended. Final detections:\n{_describe_detections(detections)}"
            else:
                obs_desc = (
                    f"Mission ended ({reason_finished}). "
                    f"No target was found during the entire search. "
                    f"You must now execute the fallback/otherwise action from the task goal."
                )
        else:
            target_dets = [d for d in detections if d.get("is_target")]
            obs_desc = _describe_detections(target_dets if target_dets else detections)

        return {"obs_description": obs_desc}

    async def _decide(self, state: MissionState) -> dict:
        """Call LLM to choose the next action."""
        decision = await self.brain.decide_observation_response(
            task_goal=state["task_goal"],
            observations=state["obs_description"],
            elapsed_s=state.get("elapsed_s", 0),
            available_actions=state.get("available_actions", []),
            actions_description=state.get("actions_description", ""),
            robot_state=f"standing={state.get('standing', False)}",
        )
        action = decision.get("action", "stop")
        reason = decision.get("reason", "")
        logger.info(f"[{self.robot.robot_id}] LLM decision: {action} — {reason} (obs={state['obs_status']})")
        return {"action": action, "reason": reason}

    async def _act(self, state: MissionState) -> dict:
        """Execute the chosen action on the robot via robot_client."""
        action = state["action"]

        if action == "MISSION_COMPLETE":
            await robot_client.speak(self.robot.host, self.robot.port, "Mission complete", robot_id=self._rid)
            return {"outcome": "completed"}

        result = await robot_client.execute_action(
            self.robot.host, self.robot.port, action, robot_id=self._rid
        )
        if result.get("status") != "error":
            await robot_client.speak(
                self.robot.host, self.robot.port, action.replace("_", " "), robot_id=self._rid
            )
            # Update standing state in registry
            if action == "stand_up":
                robot_registry.update_standing(self.robot.robot_id, True)
            elif action in ("sit_down", "stop"):
                robot_registry.update_standing(self.robot.robot_id, False)
        else:
            logger.warning(f"[{self.robot.robot_id}] Action failed: {action}")

        return {}

    def _evaluate(self, state: MissionState) -> dict:
        """Determine the outcome of this cycle."""
        action = state["action"]
        obs_status = state["obs_status"]
        has_target = state.get("has_target", False)

        if action == "MISSION_COMPLETE":
            return {"outcome": "completed"}

        if obs_status == "finished":
            return {"outcome": "completed"}

        setup_or_nav = _actions_by_category(self.robot, "posture", "movement", "look")
        if action in setup_or_nav:
            if has_target:
                logger.info(
                    f"[{self.robot.robot_id}] Positioning action '{action}' with target "
                    "— waiting for next observation"
                )
            return {"outcome": "setup" if has_target else "ignored"}

        # Gesture/response action with target = mission fulfilled
        if has_target:
            return {"outcome": "acted"}

        return {"outcome": "ignored"}

    async def run(self, state: MissionState) -> MissionState:
        """Run one cycle of the mission graph (async)."""
        return await self.graph.ainvoke(state)


# ── CentralPiAgent (replaces MissionManager) ─────────────

class CentralPiAgent:
    """
    Manages mission lifecycle and routes observations through MissionGraph.

    Replaces MissionManager that ran on the Pi. Same logic, but hardware
    commands go via robot_client (A2A) instead of local CrawlerControl.
    """

    def __init__(self, robot: RegisteredRobot, brain: LLMClient):
        self.robot = robot
        self.brain = brain
        self.mission_graph = MissionGraph(robot, brain)

        # Mission context
        self.mission_id: Optional[str] = None
        self.task_goal: Optional[str] = None
        self.mission_active = False
        self.report_all = False

        # Shortcuts for robot_client calls (auto-fills host/port/robot_id)
        self._rid = robot.robot_id
        self._host = robot.host
        self._port = robot.command_port if hasattr(robot, 'command_port') else 9004

        # Throttle
        self._last_decision_time = 0.0
        self._min_decision_interval = 2.0

        self._processing = False

        # Anti-oscillation
        self._consecutive_turns = 0

        # Patrol
        self._stop_requested = False
        self._patrol_active = False
        self._patrol_task: Optional[asyncio.Task] = None

        # Completed missions
        self._completed_mission_ids: set = set()

    async def _exec(self, action: str, params: dict = None) -> dict:
        """Execute action on this robot (shortcut with robot_id)."""
        return await robot_client.execute_action(
            self._host, self._port, action, params, robot_id=self._rid
        )

    async def _ultrasonic(self) -> Optional[float]:
        """Read ultrasonic from this robot."""
        return await robot_client.read_ultrasonic(
            self._host, self._port, robot_id=self._rid
        )

    async def _speak(self, text: str) -> dict:
        """Speak on this robot."""
        return await robot_client.speak(
            self._host, self._port, text, robot_id=self._rid
        )

    @property
    def available_actions(self) -> list:
        return self.robot.actions + ["MISSION_COMPLETE"]

    # ── Mission lifecycle ────────────────────────────────

    def set_mission(self, mission_id: str, task_goal: str, report_all: bool = False):
        self.mission_id = mission_id
        self.task_goal = task_goal
        self.mission_active = True
        self.report_all = report_all
        self._consecutive_turns = 0
        self._stop_requested = False
        logger.info(f"[{self.robot.robot_id}] Mission set: {mission_id}, goal={task_goal}, report_all={report_all}")

    def clear_mission(self):
        self.mission_id = None
        self.task_goal = None
        self.mission_active = False
        self.report_all = False
        self._stop_requested = False
        robot_registry.clear_busy(self.robot.robot_id)
        logger.info(f"[{self.robot.robot_id}] Mission cleared")

    def stop_patrol(self):
        self._stop_requested = True

    # ── Agent mode: initial actions ──────────────────────

    async def decide_initial_actions(self):
        """Run the graph with obs_status='init' to decide setup actions."""
        if not self.task_goal:
            return

        logger.info(f"[{self.robot.robot_id}] Deciding initial actions for: {self.task_goal}")
        state = self._build_state(obs_status="init")
        result = await self.mission_graph.run(state)

        action = result.get("action", "stop")
        logger.info(f"[{self.robot.robot_id}] Initial action: {action} — {result.get('reason', '')}")

    # ── Agent mode: handle detection ─────────────────────

    async def handle_detection(self, data: dict) -> dict:
        """Process detection results from detection_engine (in-process, no HTTP).

        Called by detection_engine.run_search_mission() instead of pushing via HTTP.
        Returns: {"status": ..., "action_taken": ..., "mission_ended": bool}
        """
        # Auto-activate (but not from "finished")
        if not self.mission_active:
            obs_status = data.get("status", "searching")
            if obs_status == "finished":
                return {"status": "ignored", "reason": "mission already completed"}
            mid = data.get("mission_id")
            goal = data.get("task_goal")
            if mid and mid in self._completed_mission_ids:
                return {"status": "ignored", "reason": "mission already completed"}
            if mid and goal:
                self.set_mission(mid, goal, data.get("report_all", False))
            else:
                return {"status": "ignored", "reason": "no active mission"}

        if self._processing:
            return {"status": "skipped", "reason": "already processing"}

        obs_status = data.get("status", "searching")
        detections = data.get("detections", [])
        has_target = any(d.get("is_target") for d in detections)

        # Fast path: no target and not report_all → skip
        if obs_status == "searching" and not has_target and not self.report_all:
            return {"status": "ignored", "reason": "no target detected"}

        # Report task: just log, no LLM
        if self.report_all and obs_status == "searching":
            labels = [d.get("label", "?") for d in detections]
            logger.info(f"[{self.robot.robot_id}] Report: detected {labels}")
            return {"status": "ok", "action_taken": "stop", "reason": f"logged: {labels}"}

        # Throttle (except "finished")
        now = time.time()
        if obs_status != "finished" and now - self._last_decision_time < self._min_decision_interval:
            return {"status": "throttled", "reason": "too frequent"}

        self._processing = True
        self._last_decision_time = now

        try:
            # ── Target during patrol → stop patrol, LLM takes over ──
            if has_target and not self.report_all and self._patrol_active:
                return await self._handle_target_during_patrol(data, detections)

            # ── Target found (no patrol) → LLM decides ──
            if has_target and not self.report_all:
                logger.info(f"[{self.robot.robot_id}] Target found — LLM deciding action...")
                return await self._llm_decide_and_execute(data, detections)

            # ── No target / finished → run through MissionGraph ──
            state = self._build_state(
                obs_status=obs_status,
                obs_reason=data.get("reason", ""),
                detections=detections,
                has_target=has_target,
                elapsed_s=data.get("elapsed_s", 0),
                task_goal=data.get("task_goal", self.task_goal or ""),
            )

            result = await self.mission_graph.run(state)

            action = result.get("action", "stop")
            reason = result.get("reason", "")
            outcome = result.get("outcome", "ignored")

            if outcome in ("completed", "acted"):
                if self.report_all:
                    logger.info(f"[{self.robot.robot_id}] Report task: detection logged, patrol continues")
                    return {"status": "ok", "action_taken": "stop", "reason": reason}
                else:
                    if self.mission_id:
                        self._completed_mission_ids.add(self.mission_id)
                    if obs_status == "finished":
                        self.clear_mission()
                    return {"status": "ok", "action_taken": action, "reason": reason, "mission_ended": True}

            if outcome == "setup":
                return {"status": "ok", "action_taken": action, "reason": reason}

            if obs_status == "finished":
                if self.mission_id:
                    self._completed_mission_ids.add(self.mission_id)
                self.clear_mission()
                return {"status": "ok", "action_taken": action, "reason": reason, "mission_ended": True}

            return {"status": "ok", "action_taken": action, "reason": reason}

        except Exception as e:
            logger.error(f"[{self.robot.robot_id}] Detection handling error: {e}")
            return {"status": "error", "reason": str(e)}
        finally:
            self._processing = False

    async def _handle_target_during_patrol(self, data: dict, detections: list) -> dict:
        """Handle target detection while patrol is running.
        Stops patrol and lets LLM take over."""
        logger.info(f"[{self.robot.robot_id}] Target during patrol — stopping patrol, LLM takes over")
        self._consecutive_turns = 0
        self.stop_patrol()
        for _ in range(30):  # wait up to 3s for patrol to stop
            if not self._patrol_active:
                break
            await asyncio.sleep(0.1)

        return await self._llm_decide_and_execute(data, detections)

    async def _llm_decide_and_execute(self, data: dict, detections: list) -> dict:
        """
        Ask LLM to decide next action for target interaction.

        LLM sees target position (left/right/center) and distance (far/close),
        then decides: turn toward it, move closer, or execute response gesture.

        This bypasses MissionGraph for direct target interaction (same as original).
        """
        task_goal = data.get("task_goal", self.task_goal or "")
        obs_desc = _describe_detections(
            [d for d in detections if d.get("is_target")] or detections
        )

        # Add ultrasonic distance reading
        ultrasonic_cm = await self._ultrasonic()
        if ultrasonic_cm is not None:
            obs_desc += f"\n\nUltrasonic sensor: obstacle/object at {ultrasonic_cm:.0f}cm ahead"
        else:
            obs_desc += "\n\nUltrasonic sensor: no reading"

        # After too many turns, hint LLM to stop turning and act
        if self._consecutive_turns >= 3:
            obs_desc += (
                "\n\nIMPORTANT: You have already turned multiple times. "
                "The target is approximately in front of you. "
                "Do NOT turn again. Either move forward to approach, "
                "or execute the response gesture (e.g. wave) now."
            )

        # Ask LLM (up to 2 attempts, guard against stand_up when standing)
        for attempt in range(2):
            decision = await self.brain.decide_observation_response(
                task_goal=task_goal,
                observations=obs_desc,
                elapsed_s=data.get("elapsed_s", 0),
                available_actions=self.available_actions,
                actions_description=self.robot.actions_description,
                robot_state=f"standing={self.robot.standing}",
            )
            action = decision.get("action", "stop")
            reason = decision.get("reason", "")
            logger.info(f"[{self.robot.robot_id}] LLM decision (attempt {attempt+1}): {action} — {reason}")

            if action == "stand_up" and self.robot.standing:
                logger.info(f"[{self.robot.robot_id}] Already standing, retrying")
                continue
            break

        # Gesture action → execute and complete mission
        gesture_actions = _actions_by_category(self.robot, "gesture")
        if action in gesture_actions or action == "MISSION_COMPLETE":
            self._consecutive_turns = 0
            if action != "MISSION_COMPLETE":
                await self._execute_robot_action(action)
            await self._speak("Mission complete")
            if self.mission_id:
                self._completed_mission_ids.add(self.mission_id)
            return {"status": "ok", "action_taken": action, "reason": reason, "mission_ended": True}

        # Turn action → use bbox for precise angle
        if action in ("turn_left", "turn_right", "turn_left_angle", "turn_right_angle"):
            self._consecutive_turns += 1
            target_dets = [d for d in detections if d.get("is_target")]
            bbox = target_dets[0].get("bbox", {}) if target_dets else {}
            turn_degrees = _estimate_turn_degrees(bbox)
            direction = "left" if "left" in action else "right"
            logger.info(
                f"[{self.robot.robot_id}] Turning {direction} ~{turn_degrees}° "
                f"(from bbox) [turn #{self._consecutive_turns}]"
            )
            await self._turn_by_degrees(direction, turn_degrees)
            return {"status": "ok", "action_taken": action, "reason": f"turned {direction} ~{turn_degrees}°"}

        # Other navigation (forward, backward, etc.)
        nav_actions = _actions_by_category(self.robot, "movement", "look")
        if action in nav_actions:
            self._consecutive_turns = 0
            await self._execute_robot_action(action)
            return {"status": "ok", "action_taken": action, "reason": reason}

        # stand_up
        if action == "stand_up":
            await self._execute_robot_action("stand_up")
            return {"status": "ok", "action_taken": action, "reason": reason}

        # stop or other
        logger.info(f"[{self.robot.robot_id}] LLM suggested '{action}' — waiting for next observation")
        return {"status": "ok", "action_taken": action, "reason": reason}

    # ── Patrol ───────────────────────────────────────────

    async def patrol(self) -> dict:
        """Patrol a fixed route. Detection handled by Central's detection_engine."""
        self._stop_requested = False
        self._patrol_active = True
        start = time.time()

        logger.info(f"[{self.robot.robot_id}] Patrol started")
        await self._speak("Patrol started")

        try:
            if not self.robot.standing:
                await self._execute_robot_action("stand_up")

            route = DEFAULT_PATROL_ROUTE
            for i, (action, count) in enumerate(route):
                if self._stop_requested:
                    logger.info(f"[{self.robot.robot_id}] Patrol aborted at step {i+1}")
                    break

                logger.info(f"[{self.robot.robot_id}] Patrol step {i+1}/{len(route)}: {action}({count})")

                if action == "forward":
                    await self._patrol_forward(count)
                elif action == "backward":
                    await self._exec("backward", {"steps": count})
                elif action == "turn_left":
                    await self._exec("turn_left_angle", {"steps": 2, "angle": count})
                elif action == "turn_right":
                    await self._exec("turn_right_angle", {"steps": 2, "angle": count})

            if not self._stop_requested:
                logger.info(f"[{self.robot.robot_id}] Patrol route completed")
                await self._execute_robot_action("sit_down")
        finally:
            self._patrol_active = False

        elapsed_ms = int((time.time() - start) * 1000)
        await self._speak("Patrol complete")

        if self.report_all and self.mission_active:
            return {"status": "completed", "elapsed_ms": elapsed_ms}

        return {"status": "completed", "elapsed_ms": elapsed_ms}

    async def _patrol_forward(self, steps: int):
        """Walk forward N steps with obstacle avoidance via ultrasonic."""
        walked = 0
        while walked < steps:
            if self._stop_requested:
                return

            dist = await self._ultrasonic()
            if dist is not None and dist <= 20:  # obstacle threshold
                if self._stop_requested:
                    return
                logger.info(f"[{self.robot.robot_id}] Obstacle at {dist:.0f}cm, asking LLM")
                await self._llm_decide_obstacle(dist)
                if self._stop_requested:
                    return
            else:
                await self._exec("forward", {"steps": 1})
                walked += 1

    async def _llm_decide_obstacle(self, distance_cm: float):
        """Ask LLM how to handle an obstacle during patrol."""
        parsed = await self.brain.decide_obstacle_response(distance_cm)
        action = parsed.get("action", "turn_left")
        reason = parsed.get("reason", "")
        logger.info(f"[{self.robot.robot_id}] Obstacle decision: {action} — {reason}")

        if action == "backward":
            await self._exec("backward", {"steps": 2})
        elif action == "turn_left":
            await self._exec("turn_left_angle", {"steps": 2, "angle": 45})
            await self._exec("forward", {"steps": 2})
        elif action == "turn_right":
            await self._exec("turn_right_angle", {"steps": 2, "angle": 45})
            await self._exec("forward", {"steps": 2})
        else:
            await self._exec("backward", {"steps": 1})

    # ── Direct control: execute steps ────────────────────

    async def execute_steps(self, steps: list) -> dict:
        """Execute a list of action steps sequentially."""
        results = []
        total_time = 0

        for i, step in enumerate(steps):
            action = step.get("action", step) if isinstance(step, dict) else step
            logger.info(f"[{self.robot.robot_id}] Step {i+1}/{len(steps)}: {action}")

            result = await self._exec(action)
            duration = result.get("duration_ms", 0)
            total_time += duration
            status = "completed" if result.get("status") == "ok" else "failed"
            results.append({"step": i + 1, "action": action, "status": status})

            await self._speak(action.replace("_", " "))

        summaries = [f"{r['step']}. {r['action']}: {r['status']}" for r in results]
        return {
            "agent_id": self.robot.robot_id,
            "detections": [],
            "mission_time_ms": total_time,
            "summary": f"Executed {len(steps)} steps:\n" + "\n".join(summaries),
            "mission_status": "completed" if all(r["status"] == "completed" for r in results) else "partial",
        }

    async def execute_steps_from_prompt(self, prompt: str) -> dict:
        """Decompose a natural language prompt into actions and execute them."""
        actions = await self.brain.decompose_actions(prompt, self.robot.actions)
        steps = [{"action": a} for a in actions]
        return await self.execute_steps(steps)

    # ── Hardware helpers ─────────────────────────────────

    async def _execute_robot_action(self, action: str, params: dict = None):
        """Execute a single action on the robot and update state."""
        result = await self._exec(action, params)
        if action == "stand_up":
            robot_registry.update_standing(self.robot.robot_id, True)
        elif action in ("sit_down", "stop"):
            robot_registry.update_standing(self.robot.robot_id, False)
        return result

    async def _turn_by_degrees(self, direction: str, degrees: float):
        """Turn by estimated degrees using turn_left_angle/turn_right_angle."""
        if degrees <= 45:
            angle_param = int(degrees / 0.55)
            steps = 1
        else:
            angle_param = int(degrees / 0.55 / 2)
            steps = 2
        angle_param = max(10, min(angle_param, 90))

        action = "turn_left_angle" if direction == "left" else "turn_right_angle"
        await self._exec(action, {"steps": steps, "angle": angle_param})

    # ── State builder ────────────────────────────────────

    def _build_state(self, obs_status="searching", **kwargs) -> MissionState:
        """Build MissionState dict for graph invocation."""
        return {
            "task_goal": kwargs.get("task_goal", self.task_goal or ""),
            "mission_id": self.mission_id or "",
            "robot_id": self.robot.robot_id,
            "obs_status": obs_status,
            "obs_reason": kwargs.get("obs_reason", ""),
            "detections": kwargs.get("detections", []),
            "has_target": kwargs.get("has_target", False),
            "elapsed_s": kwargs.get("elapsed_s", 0),
            "standing": self.robot.standing,
            "available_actions": self.available_actions,
            "actions_description": self.robot.actions_description,
            "obs_description": "",
            "action": "",
            "reason": "",
            "outcome": "",
        }


# ── Pi Agent Manager ─────────────────────────────────────

class PiAgentManager:
    """Manages CentralPiAgent instances — one per robot."""

    def __init__(self):
        self._agents: dict[str, CentralPiAgent] = {}
        self._brain = LLMClient()

    def get_or_create(self, robot: RegisteredRobot) -> CentralPiAgent:
        """Get existing agent or create a new one for this robot."""
        if robot.robot_id not in self._agents:
            self._agents[robot.robot_id] = CentralPiAgent(robot, self._brain)
            logger.info(f"Created CentralPiAgent for {robot.robot_id}")
        return self._agents[robot.robot_id]

    def get(self, robot_id: str) -> Optional[CentralPiAgent]:
        return self._agents.get(robot_id)

    def remove(self, robot_id: str):
        agent = self._agents.pop(robot_id, None)
        if agent:
            agent.stop_patrol()
            logger.info(f"Removed CentralPiAgent for {robot_id}")


# Singleton
pi_agent_manager = PiAgentManager()
