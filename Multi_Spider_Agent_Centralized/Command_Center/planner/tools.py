"""
Planner Agent tools for multi-robot ReAct coordination.

Tool definitions (OpenAI function calling format) + implementations + dispatch.
"""

import asyncio
import json
import logging
import uuid

# LOCALIZATION: nearest-robot / zone-adjacency helpers disabled.
# from ..infra.position_tracker import find_nearest_robot, are_adjacent, get_position
from ..infra.robot_registry import robot_registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions (for LLM)
# ---------------------------------------------------------------------------

TOOL_SPECS = [
    {
        "name": "dispatch_patrol",
        "description": "Start patrol on specified robots. Call this first to begin the search mission.",
        "parameters": {
            "type": "object",
            "properties": {
                "robot_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of robot IDs to dispatch",
                },
            },
            "required": ["robot_ids"],
        },
    },
    {
        "name": "wait_for_detection",
        "description": "Wait for the next robot to complete and report results. Returns detection info or 'no target'.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "execute_action",
        "description": "Make a robot perform a physical action (e.g. wave, sit_down, stop).",
        "parameters": {
            "type": "object",
            "properties": {
                "robot_id": {"type": "string", "description": "Robot ID"},
                "action": {"type": "string", "description": "Action name (wave, sit_down, stop, etc.)"},
            },
            "required": ["robot_id", "action"],
        },
    },
    {
        "name": "cancel_robot",
        "description": "Cancel a robot's patrol and stop it.",
        "parameters": {
            "type": "object",
            "properties": {
                "robot_id": {"type": "string", "description": "Robot ID to cancel"},
            },
            "required": ["robot_id"],
        },
    },
    # LOCALIZATION: dispatch_nearest_support depends on AprilTag positions.
    # {
    #     "name": "dispatch_nearest_support",
    #     "description": (
    #         "Dispatch the nearest idle robot to support another robot that found a target. "
    #         "Use this after wait_for_detection reports a hit. The nearest available robot "
    #         "will travel to the target's location to assist."
    #     ),
    #     "parameters": {
    #         "type": "object",
    #         "properties": {
    #             "finder_robot_id": {
    #                 "type": "string",
    #                 "description": "The robot that found the target (stays on-site)."
    #             },
    #             "reason": {
    #                 "type": "string",
    #                 "description": "Brief reason for requesting support."
    #             },
    #         },
    #         "required": ["finder_robot_id"],
    #     },
    # },
    {
        "name": "finish",
        "description": "End the coordination. Call this when the mission is complete.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief summary of what happened"},
            },
            "required": ["summary"],
        },
    },
]

OPENAI_TOOLS = [{"type": "function", "function": spec} for spec in TOOL_SPECS]


# ---------------------------------------------------------------------------
# Tool executor (holds coordination state)
# ---------------------------------------------------------------------------

class CoordinationToolExecutor:
    """Manages shared state and executes tools during a coordinated patrol.

    Created per coordination session (one per multi-robot task).
    """

    def __init__(self, robots: list[dict], phase: dict, prompt: str,
                 dispatch_fn, cancel_fn, direct_control_fn):
        self.robots = robots
        self.phase = phase
        self.prompt = prompt
        self._dispatch_to_robot = dispatch_fn
        self._cancel_robot = cancel_fn
        self._dispatch_direct_control = direct_control_fn

        # Shared state
        self.patrol_tasks: dict = {}       # robot_id → (asyncio.Task, robot_dict)
        self.detection_events: list = []   # [{robot_id, zone, detections, result}]
        self.agent_results: list = []      # final results to return
        self.completed: set = set()        # robot_ids that finished
        self.support_tasks: dict = {}      # robot_id → asyncio.Task (fire-and-forget dispatches)

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Dispatch a tool call by name. Returns result string."""
        if tool_name == "dispatch_patrol":
            return await self._dispatch_patrol(arguments.get("robot_ids", []))
        elif tool_name == "wait_for_detection":
            return await self._wait_for_detection()
        elif tool_name == "execute_action":
            return await self._execute_action(arguments["robot_id"], arguments["action"])
        elif tool_name == "cancel_robot":
            return await self._cancel(arguments["robot_id"])
        # LOCALIZATION: dispatch_nearest_support disabled.
        # elif tool_name == "dispatch_nearest_support":
        #     return await self._dispatch_nearest_support(
        #         arguments["finder_robot_id"],
        #         arguments.get("reason", ""),
        #     )
        elif tool_name == "finish":
            return arguments.get("summary", "done")
        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    async def _dispatch_patrol(self, robot_ids: list[str]) -> str:
        dispatched = []
        for rid in robot_ids:
            robot = next((r for r in self.robots if r["id"] == rid), None)
            if not robot or rid in self.patrol_tasks:
                continue
            task_payload = {
                "task_type": "agent",
                "mission_id": str(uuid.uuid4())[:8],
                "target_type": self.phase.get("target_type", "object"),
                "target_name": self.phase.get("target_name", ""),
                "report_all": self.phase.get("report_all", False),
                "task_goal": self.prompt,
            }
            coro = self._dispatch_to_robot(rid, task_payload, timeout=300)
            self.patrol_tasks[rid] = (asyncio.create_task(coro), robot)
            dispatched.append(rid)
        return f"Patrol started on: {dispatched}"

    async def _wait_for_detection(self) -> str:
        pending = set(t for rid, (t, _) in self.patrol_tasks.items() if rid not in self.completed)
        if not pending:
            return "No robots are patrolling."

        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        results_text = []
        for finished_task in done:
            for rid, (task, robot) in self.patrol_tasks.items():
                if task is finished_task:
                    try:
                        result = task.result()
                    except Exception as e:
                        result = {"status": "error", "error": str(e)}
                    self.completed.add(rid)

                    mission_result = result.get("mission_result", {})
                    dets = mission_result.get("detections", [])
                    zone = robot.get("zone", "?")

                    if mission_result.get("status") == "completed" and len(dets) > 0:
                        self.detection_events.append({
                            "robot_id": rid, "robot": robot,
                            "zone": zone, "detections": dets, "result": result,
                        })
                        labels = [f"{d.get('label', '?')}({d.get('confidence', 0):.0%})" for d in dets[:5]]
                        results_text.append(f"{rid} (zone {zone}): FOUND {', '.join(labels)}")
                    else:
                        status = mission_result.get("status", result.get("status", "?"))
                        results_text.append(f"{rid} (zone {zone}): {status}, no target detected")
                    break

        still_patrolling = [rid for rid in self.patrol_tasks if rid not in self.completed]
        summary = "\n".join(results_text)
        summary += f"\nStill patrolling: {still_patrolling if still_patrolling else 'none'}"
        return summary

    async def _execute_action(self, robot_id: str, action: str) -> str:
        robot = next((r for r in self.robots if r["id"] == robot_id), None)
        if not robot:
            return f"Robot {robot_id} not found"
        await self._dispatch_direct_control(robot, action)
        return f"{robot_id}: executed {action}"

    async def _cancel(self, robot_id: str) -> str:
        await self._cancel_robot(robot_id)
        self.completed.add(robot_id)
        if robot_id in self.patrol_tasks:
            task, _ = self.patrol_tasks[robot_id]
            task.cancel()
        return f"{robot_id}: cancelled"

    # LOCALIZATION: _dispatch_nearest_support depends on position/zone helpers.
    # async def _dispatch_nearest_support(self, finder_robot_id: str, reason: str) -> str:
    #     """Find the nearest idle robot and dispatch it to the finder's location."""
    #     finder_pos = get_position(finder_robot_id)
    #     if finder_pos is None:
    #         return (
    #             f"Cannot dispatch support: finder {finder_robot_id} has no known position. "
    #             "Make sure AprilTags are visible and tag_positions.json is configured."
    #         )
    #
    #     exclude = (
    #         [finder_robot_id]
    #         + list(self.patrol_tasks.keys())
    #         + list(self.support_tasks.keys())
    #     )
    #     nearest = find_nearest_robot(finder_pos, exclude_ids=exclude, require_idle=True)
    #     if nearest is None:
    #         return f"No idle robot available to support {finder_robot_id}."
    #
    #     support_id = nearest["robot_id"]
    #     support_robot = robot_registry.get_robot(support_id)
    #     if support_robot is None:
    #         return f"Support robot {support_id} was unregistered before dispatch."
    #     finder_robot = robot_registry.get_robot(finder_robot_id)
    #
    #     adj_note = ""
    #     if finder_robot and support_robot.zone and finder_robot.zone:
    #         if not are_adjacent(support_robot.zone, finder_robot.zone):
    #             adj_note = (
    #                 f" (note: {support_robot.zone} and {finder_robot.zone} are not "
    #                 "marked adjacent in zone_adjacency.json; traversal may need a relay)"
    #             )
    #
    #     support_dict = {
    #         "id": support_id,
    #         "host": support_robot.host,
    #         "port": support_robot.port,
    #         "zone": support_robot.zone,
    #         "robot_type": support_robot.robot_type,
    #     }
    #     finder_zone = finder_robot.zone if finder_robot else "?"
    #     nav_prompt = (
    #         f"Head to zone {finder_zone} to support {finder_robot_id}. "
    #         f"Move toward world position ({finder_pos[0]:.2f}, {finder_pos[1]:.2f}). "
    #         f"Use visible AprilTags as waypoints."
    #     )
    #
    #     support_mission_id = f"support-{uuid.uuid4().hex[:8]}"
    #     robot_registry.set_busy(support_id, support_mission_id)
    #
    #     async def _do_support_dispatch():
    #         try:
    #             await asyncio.wait_for(
    #                 self._dispatch_direct_control(support_dict, nav_prompt),
    #                 timeout=30,
    #             )
    #             logger.info(f"Support dispatch to {support_id} acknowledged")
    #         except asyncio.TimeoutError:
    #             logger.warning(f"Support dispatch to {support_id} timed out after 30s")
    #         except asyncio.CancelledError:
    #             logger.info(f"Support dispatch to {support_id} cancelled")
    #             raise
    #         except Exception as e:
    #             logger.error(f"Support dispatch to {support_id} failed: {e}")
    #         finally:
    #             robot_registry.clear_busy(support_id)
    #
    #     self.support_tasks[support_id] = asyncio.create_task(_do_support_dispatch())
    #
    #     return (
    #         f"Support dispatched: {support_id} → {finder_robot_id} "
    #         f"(distance={nearest['distance']:.2f}m){adj_note}"
    #     )
