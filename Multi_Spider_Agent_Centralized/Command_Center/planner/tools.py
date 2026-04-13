"""
Planner Agent tools for multi-robot ReAct coordination.

Tool definitions (OpenAI function calling format) + implementations + dispatch.
"""

import asyncio
import json
import logging
import uuid

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
# VLM tools (used during intent parsing)
# ---------------------------------------------------------------------------

VLM_TOOL_SPECS = [
    {
        "name": "enable_vlm",
        "description": (
            "Enable the Vision Language Model for this task. "
            "Call this when the task needs visual understanding beyond simple object detection. "
            "Use mode='verify' to match attributes (e.g. 'find person wearing green shirt'). "
            "Use mode='describe' to describe appearance (e.g. 'describe what you see'). "
            "Do NOT call for simple patrol, counting, or find-and-act without visual requirements."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["verify", "describe"],
                    "description": "verify = match specific attributes, describe = describe appearance in detail",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason why VLM is needed",
                },
            },
            "required": ["mode", "reason"],
        },
    },
]

OPENAI_VLM_TOOLS = [{"type": "function", "function": spec} for spec in VLM_TOOL_SPECS]


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
