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
import random
import time
from typing import Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from .brain import LLMClient
from .target_approach import SpiderTargetApproach
from ..infra.robot_client import robot_client
from ..infra.robot_registry import robot_registry, RegisteredRobot

logger = logging.getLogger(__name__)

# ── Constants (from graph.py) ─────────────────────────────
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
CAMERA_H_FOV = 62.0

def _actions_by_category(robot: "RegisteredRobot", *categories: str) -> set:
    """Get action names from a robot's capabilities by category.

    Raises if capabilities have not been fetched from the robot.
    """
    caps = robot.capabilities.get("actions", []) if robot.capabilities else []
    if not caps:
        raise RuntimeError(
            f"Robot {robot.robot_id} has no capabilities metadata. "
            "Ensure /capabilities was fetched from the Thin Client before starting a mission."
        )
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

    def __init__(self, robot: RegisteredRobot, brain: LLMClient, on_reasoning=None):
        self.robot = robot
        self.brain = brain
        self._rid = robot.robot_id
        self._on_reasoning = on_reasoning
        self.graph = self._build()

    async def _emit(self, step: str):
        if self._on_reasoning:
            await self._on_reasoning(self._rid, step)

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
        await self._emit(f"Intent: {action} — {reason}")
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
        self.on_reasoning = None  # async callback(robot_id, step_str)
        self.mission_graph = MissionGraph(robot, brain)

        # Target approach module (Role 2 — portable, will move to turtlebot)
        self._target_approach = SpiderTargetApproach(robot, brain)

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

        # Patrol detection log (for report generation)
        self._detections_log: list = []

        # Completed missions
        self._completed_mission_ids: set = set()

        # Last emitted approach action (avoid repeating same emit)
        self._last_approach_emit: str = ""

        # Report mode dedup: track_ids already logged, last emitted labels
        self._seen_track_ids: set = set()
        self._last_report_labels: list = []

    async def _emit(self, step: str):
        """Emit a reasoning step to the callback (if set)."""
        if self.on_reasoning:
            await self.on_reasoning(self._rid, step)
        # Also forward to MissionGraph
        self.mission_graph._on_reasoning = self.on_reasoning

    async def _exec(self, action: str, params: dict = None) -> dict:
        """Execute action on this robot (shortcut with robot_id).
        Skip commands during patrol shutdown to prevent leaking commands after cancel.
        """
        if self._stop_requested and self._patrol_active:
            return {"status": "skipped", "reason": "patrol stopping"}
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
        self._target_approach.clear()
        self._detections_log = []
        self._seen_track_ids = set()
        self._last_report_labels = []
        self._last_approach_emit = ""
        logger.info(f"[{self.robot.robot_id}] Mission set: {mission_id}, goal={task_goal}, report_all={report_all}")

    def clear_mission(self):
        self.mission_id = None
        self.task_goal = None
        self.mission_active = False
        self.report_all = False
        self._stop_requested = False
        self._target_approach.clear()
        logger.info(f"[{self.robot.robot_id}] Mission cleared")

    def stop_patrol(self):
        self._stop_requested = True

    # ── Agent mode: initial actions ──────────────────────

    async def decide_initial_actions(self):
        """Setup actions at mission start. Stand up if not already standing."""
        if not self.task_goal:
            return

        if not self.robot.standing:
            logger.info(f"[{self.robot.robot_id}] Initial setup: stand_up")
            await self._emit("Auto: stand_up (initial setup)")
            await self._execute_robot_action("stand_up")
        else:
            logger.info(f"[{self.robot.robot_id}] Already standing, skipping init")

    # ── Agent mode: handle detection ─────────────────────

    async def handle_detection(self, data: dict) -> dict:
        """Process detection results from detection_engine.

        Routes to:
        - Role 1 (report_all): log detections, no approach
        - Role 2 (conditional action): TargetApproach module
        - Fallback (finished, no target): MissionGraph LLM for fallback action
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

        # ── Role 1: Report mode — log and continue ──
        if self.report_all and obs_status == "searching":
            if detections:
                for d in detections:
                    # Deduplicate by track_id — only log first sighting of each tracked object
                    track_id = d.get("track_id")
                    label = d.get("label", "?")
                    if track_id is not None:
                        seen_key = f"{label}:{track_id}"
                        if seen_key in self._seen_track_ids:
                            continue
                        self._seen_track_ids.add(seen_key)
                    self._detections_log.append({
                        "label": label,
                        "confidence": d.get("confidence", 0),
                        "time_s": data.get("elapsed_s", 0),
                        "position": _describe_bbox(d.get("bbox", [])),
                        "track_id": track_id,
                    })
                # Only emit when new unique detections were added
                new_labels = sorted(set(d.get("label", "?") for d in detections))
                if new_labels != self._last_report_labels:
                    logger.info(f"[{self._rid}] Report: detected {new_labels}")
                    await self._emit(f"Result: detected {new_labels}")
                    self._last_report_labels = new_labels
            return {"status": "ok", "action_taken": "stop", "reason": "logged"}

        # Fast path: no target → skip
        if obs_status == "searching" and not has_target:
            return {"status": "ignored", "reason": "no target detected"}

        # Throttle (except "finished")
        now = time.time()
        if obs_status != "finished" and now - self._last_decision_time < self._min_decision_interval:
            return {"status": "throttled", "reason": "too frequent"}

        self._processing = True
        self._last_decision_time = now

        try:
            # ── Mission finished → LLM decides fallback action ──
            if obs_status == "finished":
                return await self._handle_finished(data, detections, has_target)

            # ── Role 2: Target found → TargetApproach module ──
            # Only switch to approach when target is close enough.
            # Otherwise let patrol bring us closer first.
            if has_target:
                from .target_approach import _get_bbox_height, FRAME_HEIGHT
                from ..config import APPROACH_DIST_CM, APPROACH_BBOX_RATIO

                target_dets = [d for d in detections if d.get("is_target")]
                target_bbox_h = max(
                    (_get_bbox_height(d.get("bbox")) or 0) for d in target_dets
                ) if target_dets else 0

                # Check distance: prefer ultrasonic (accurate), fallback to bbox
                close_enough = False
                if not self._patrol_active:
                    close_enough = True  # no patrol running, approach regardless
                else:
                    ultrasonic_cm = await self._ultrasonic()
                    if ultrasonic_cm is not None and ultrasonic_cm <= APPROACH_DIST_CM:
                        close_enough = True  # ultrasonic confirms close
                    elif target_bbox_h >= FRAME_HEIGHT * APPROACH_BBOX_RATIO:
                        close_enough = True  # bbox suggests close (ultrasonic may not see target)

                if close_enough:
                    return await self._handle_target(data, detections)
                else:
                    logger.info(
                        f"[{self._rid}] Target detected but far "
                        f"(bbox_h={target_bbox_h:.0f}px, ultrasonic={ultrasonic_cm}cm), "
                        f"continuing patrol"
                    )
                    return {"status": "ok", "reason": "target far, continuing patrol"}

            return {"status": "ok", "action_taken": "stop", "reason": "waiting"}

        except Exception as e:
            logger.error(f"[{self._rid}] Detection handling error: {e}")
            await self._emit(f"Auto: Error — {e}")
            return {"status": "error", "reason": str(e)}
        finally:
            self._processing = False

    async def _handle_target(self, data: dict, detections: list) -> dict:
        """Handle target detection using TargetApproach module.

        Stage 2: LLM plans once (approach? what action?)
        Stage 3: bbox rules navigate, execute when close
        """
        # Stop patrol if running
        if self._patrol_active:
            logger.info(f"[{self._rid}] Target during patrol — stopping patrol")
            await self._emit("Auto: Target detected — stopping patrol")
            self.stop_patrol()
            for _ in range(30):
                if not self._patrol_active:
                    break
                await asyncio.sleep(0.1)

        task_goal = data.get("task_goal", self.task_goal or "")
        target_dets = [d for d in detections if d.get("is_target")]
        if target_dets:
            label = target_dets[0].get("label", "unknown")
            # If VLM confirmed this target, tell LLM it matches the task description
            if "VLM confirmed" in label or target_dets[0].get("source", "").endswith("+vlm"):
                detected_label = f"person (confirmed match for task: {task_goal[:50]})"
            else:
                detected_label = label
        else:
            detected_label = "unknown"

        # Stage 2: Plan (LLM, called once, cached)
        first_plan = not self._target_approach.has_plan
        plan = await self._target_approach.plan(task_goal, detected_label)
        if first_plan:
            await self._emit(f"Intent: Target '{detected_label}' → {plan['final_action']} (approach={plan['approach']}) — {plan['reason']}")

        # No approach needed → execute immediately and finish
        if not plan["approach"]:
            action = plan["final_action"]
            await self._target_approach.execute_action(action)
            if self.mission_id:
                self._completed_mission_ids.add(self.mission_id)
            return {"status": "ok", "action_taken": action, "reason": plan["reason"], "mission_ended": True}

        # Stage 3: Approach step (rules, per frame)
        ultrasonic_cm = await self._ultrasonic()
        action = self._target_approach.approach_step(detections, ultrasonic_cm)

        if action == "stop":
            # Target lost — wait for next frame
            return {"status": "ok", "action_taken": "stop", "reason": "target lost, waiting"}

        # Check if it's the final action (close enough)
        if action == plan["final_action"]:
            # Ensure standing before gesture
            if not self.robot.standing:
                await self._execute_robot_action("stand_up")
            await self._emit(f"Tool: {action} (target reached)")
            await self._target_approach.execute_action(action)
            self._consecutive_turns = 0
            if self.mission_id:
                self._completed_mission_ids.add(self.mission_id)
            return {"status": "ok", "action_taken": action, "reason": "target reached", "mission_ended": True}

        # Navigation action (turn / forward) — only emit when action changes
        if action in ("turn_left", "turn_right"):
            self._consecutive_turns += 1
            degrees = self._target_approach.get_turn_degrees(detections)
            direction = "left" if action == "turn_left" else "right"
            logger.info(f"[{self._rid}] Approach: turn {direction} ~{degrees}° [#{self._consecutive_turns}]")
            emit_key = f"turn_{direction}"
            if emit_key != self._last_approach_emit:
                await self._emit(f"Auto: turn {direction} ~{degrees}° (approaching)")
                self._last_approach_emit = emit_key
            await self._turn_by_degrees(direction, degrees)
        elif action == "forward":
            self._consecutive_turns = 0
            if self._last_approach_emit != "forward":
                await self._emit("Auto: forward (approaching)")
                self._last_approach_emit = "forward"
            await self._execute_robot_action("forward")
        else:
            await self._emit(f"Auto: {action} (approaching)")
            await self._execute_robot_action(action)

        return {"status": "ok", "action_taken": action, "reason": "approaching target"}

    async def _handle_finished(self, data: dict, detections: list, has_target: bool) -> dict:
        """Handle mission finished — just clear mission state.

        sit_down responsibility:
        - Normal patrol completion: patrol() method does sit_down
        - Cancelled by coordination: Planner sends sit_down
        """
        reason = data.get("reason", "mission finished")
        await self._emit(f"Auto: Mission finished — {reason}")

        if self.mission_id:
            self._completed_mission_ids.add(self.mission_id)
        self.clear_mission()
        return {"status": "ok", "action_taken": "none", "reason": reason, "mission_ended": True}

    # ── Patrol ───────────────────────────────────────────

    async def patrol(self) -> dict:
        """Patrol using LLM-planned route. Detection handled by Central's detection_engine."""
        self._stop_requested = False
        self._patrol_active = True
        start = time.time()

        logger.info(f"[{self.robot.robot_id}] Patrol started")
        await self._emit("Auto: Patrol started")

        try:
            if not self.robot.standing:
                await self._execute_robot_action("stand_up")

            # LLM plans the route based on task_goal
            route_plan = await self.brain.plan_patrol_route(self.task_goal or "patrol area")

            # Target may have been found while LLM was planning — skip if already stopped
            if self._stop_requested:
                logger.info(f"[{self.robot.robot_id}] Target found during route planning — skipping patrol")
                self._patrol_active = False
                return {"status": "completed", "elapsed_ms": int((time.time() - start) * 1000)}

            pattern = route_plan.get("pattern", "area")
            steps = route_plan.get("steps", [])
            await self._emit(f"Intent: route plan → {pattern}, {len(steps)} steps — {route_plan.get('reason', '')}")

            if pattern == "stationary" or not steps:
                from ..config import MISSION_TIMEOUT_STATIONARY
                logger.info(f"[{self.robot.robot_id}] Stationary patrol — observing for {MISSION_TIMEOUT_STATIONARY}s")
                await self._emit(f"Auto: Observing for {MISSION_TIMEOUT_STATIONARY}s (stationary)")
                # Wait long enough for detection engine to collect observations
                for _ in range(MISSION_TIMEOUT_STATIONARY):
                    if self._stop_requested:
                        break
                    await asyncio.sleep(1)
                self._patrol_active = False
                return {"status": "completed", "elapsed_ms": int((time.time() - start) * 1000)}

            # Convert LLM steps to (action, value) tuples
            route = [(s.get("action", "forward"), s.get("value", 1)) for s in steps]

            for i, (action, count) in enumerate(route):
                if self._stop_requested:
                    logger.info(f"[{self.robot.robot_id}] Patrol aborted at step {i+1}")
                    await self._emit(f"Auto: Patrol aborted at step {i+1}")
                    break

                logger.info(f"[{self.robot.robot_id}] Patrol step {i+1}/{len(route)}: {action}({count})")
                await self._emit(f"Tool: {action}({count})")

                if action == "forward":
                    await self._patrol_forward(count)
                elif action == "backward":
                    await self._exec("backward", {"steps": count})
                elif action == "turn_left":
                    await self._turn_by_degrees("left", count)
                elif action == "turn_right":
                    await self._turn_by_degrees("right", count)
                elif action == "pause":
                    # Hold position for `count` seconds to let detection capture stable frames
                    await self._emit(f"Auto: pausing {count}s for detection")
                    for _ in range(int(count)):
                        if self._stop_requested:
                            break
                        await asyncio.sleep(1)

            if not self._stop_requested:
                logger.info(f"[{self.robot.robot_id}] Patrol route completed")
                await self._emit("Auto: Patrol route completed")
                await self._execute_robot_action("sit_down")
        finally:
            self._patrol_active = False

        elapsed_ms = int((time.time() - start) * 1000)
        await self._speak("Patrol complete")

        # Generate structured patrol report if we logged detections
        patrol_report = None
        if self._detections_log:
            try:
                await self._emit("Intent: Generating patrol report...")
                patrol_report = await self.brain.generate_patrol_report(
                    task_goal=self.task_goal or "patrol",
                    detections_log=self._detections_log,
                )
                logger.info(f"[{self._rid}] Patrol report: {patrol_report.get('summary', '')[:100]}")
                await self._emit(f"Result: {patrol_report.get('summary', 'Report generated')}")
            except Exception as e:
                logger.warning(f"[{self._rid}] Patrol report generation failed: {e}")

        result = {"status": "completed", "elapsed_ms": elapsed_ms}
        if patrol_report:
            result["patrol_report"] = patrol_report
        return result

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
                await self._avoid_obstacle(dist)
                if self._stop_requested:
                    return
            else:
                await self._exec("forward", {"steps": 1})
                walked += 1

    async def _avoid_obstacle(self, distance_cm: float):
        """Rule-based obstacle avoidance. No LLM needed — ultrasonic has no directional info."""
        if distance_cm < 10:
            logger.info(f"[{self._rid}] Obstacle at {distance_cm:.0f}cm — backing up")
            await self._emit(f"Auto: backward (obstacle at {distance_cm:.0f}cm)")
            await self._exec("backward", {"steps": 2})
        else:
            direction = random.choice(["turn_left", "turn_right"])
            logger.info(f"[{self._rid}] Obstacle at {distance_cm:.0f}cm — {direction}")
            await self._emit(f"Auto: {direction} (obstacle at {distance_cm:.0f}cm)")
            angle_action = f"{direction}_angle"
            await self._exec(angle_action, {"steps": 2, "angle": 45})
            await self._exec("forward", {"steps": 2})

    # ── Direct control: execute steps ────────────────────

    async def execute_steps(self, steps: list) -> dict:
        """Execute a list of action steps sequentially.

        Each step is a dict with 'action' and optional 'params', e.g.:
            {"action": "turn_left", "params": {"degrees": 90}}
            {"action": "forward", "params": {"steps": 3}}
        turn_left/turn_right are dispatched through _turn_by_degrees (calibration in one place).
        """
        results = []
        total_time = 0

        for i, step in enumerate(steps):
            if isinstance(step, dict):
                action = step.get("action", "stop")
                params = step.get("params") or {}
            else:
                action = step
                params = {}
            logger.info(f"[{self.robot.robot_id}] Step {i+1}/{len(steps)}: {action} {params or ''}")
            await self._emit(f"Tool: {action}")

            if action in ("turn_left", "turn_right"):
                direction = "left" if action == "turn_left" else "right"
                degrees = params.get("degrees", 30)
                await self._turn_by_degrees(direction, degrees)
                result = {"status": "ok", "duration_ms": 0}
            else:
                result = await self._exec(action, params or None)
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
        steps, reason = await self.brain.decompose_actions(
            prompt, self.robot.actions, self.robot.actions_description
        )
        action_names = [s.get("action", s) if isinstance(s, dict) else s for s in steps]
        await self._emit(f"Intent: decompose → {action_names} — {reason}")
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
        """Turn by estimated degrees using turn_left_angle/turn_right_angle.

        Calibration is per-robot, read from capabilities at registration time
        (robot.deg_per_step, robot.angle_param). Defaults: 30°/step, angle=25.
        Each step is sent individually so _stop_requested can interrupt between steps.
        """
        deg_per_step = self.robot.deg_per_step
        angle_param = self.robot.angle_param

        steps = max(1, round(degrees / deg_per_step))
        steps = min(steps, 12)  # cap at 12 steps (360°)

        action = "turn_left_angle" if direction == "left" else "turn_right_angle"
        for _ in range(steps):
            if self._stop_requested:
                return
            await self._exec(action, {"steps": 1, "angle": angle_param})
            # Pause after each 30° sub-step so the camera captures stable frames
            # at every position during a spin (12 stops per 360°).
            await asyncio.sleep(1.5)

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
        self._brains: dict[str, LLMClient] = {}  # one brain per robot_type

    def _get_brain(self, robot_type: str) -> LLMClient:
        """Get or create a brain (LLMClient) for this robot type."""
        if robot_type not in self._brains:
            self._brains[robot_type] = LLMClient(robot_type=robot_type)
        return self._brains[robot_type]

    def get_or_create(self, robot: RegisteredRobot) -> CentralPiAgent:
        """Get existing agent or create a new one for this robot."""
        if robot.robot_id not in self._agents:
            brain = self._get_brain(robot.robot_type)
            self._agents[robot.robot_id] = CentralPiAgent(robot, brain)
            logger.info(f"Created CentralPiAgent for {robot.robot_id} [{robot.robot_type}]")
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
