"""
LangGraph-based Planner for Multi-Robot System.

Dispatches tasks to Robot Agents via NATS (each Robot Agent is an independent process).
Falls back to in-process execution if NATS dispatch fails.

Architecture:
- Planner discovers Robot Agents via ADS
- Planner sends tasks to Robot Agents via NATS request/reply
- Each Robot Agent manages its own CentralPiAgent + Detection Engine
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


from ..infra.discovery import AgentDiscovery
from ..infra.robot_registry import robot_registry
from .llm_config import create_llm

logger = logging.getLogger(__name__)

MAX_REPLANS = 3


# ---------------------------------------------------------------------------
# Structured Outputs
# ---------------------------------------------------------------------------

class Phase(BaseModel):
    """A single execution phase describing what robots should do."""
    target_robot_ids: List[str] = Field(
        default_factory=list,
        description="Robot IDs for this phase. Empty = all online robots",
    )
    target_zone: str = Field(
        default="",
        description="Target zone letter (e.g. 'A', 'B'). Empty = no zone filter",
    )
    task_type: str = Field(description="direct_control or agent")
    target_type: str = Field(default="none", description="face, color, qr_code, object, any, or none")
    search_pattern: str = Field(default="stationary", description="'stationary' or 'patrol'")
    action: str = Field(default="none", description="Control action or 'none'")
    description: str = Field(description="One-line description")
    target_name: str = Field(default="", description="For face search: specific person name")
    report_all: bool = Field(default=False, description="True if observe/report task")
    on_detect_action: str = Field(
        default="",
        description="Action for the detecting robot after detection (e.g. 'wave'). Empty = just report zone. Only used when report_all=false.",
    )
    on_detect_others_action: str = Field(
        default="sit_down",
        description="What other robots should do when one detects the target: 'sit_down' (default), 'stop' (stand still), or 'continue' (keep patrolling).",
    )
    duration_s: int = Field(
        default=0,
        description="Duration in seconds if user specifies (e.g. 'for 5 seconds' → 5). 0 = use default timeout.",
    )


class TaskIntent(BaseModel):
    is_valid_task: bool = Field(description="False if prompt is unrelated to robot control")
    rejection_reason: str = Field(default="", description="If is_valid_task=false, reason why")
    phases: List[Phase] = Field(description="Exactly ONE phase describing the task. Always output a list with one element.")
    urgency: str = Field(description="low / medium / high")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class PlannerState(TypedDict, total=False):
    prompt: str
    strategy: str
    iteration: int
    start_time: float
    intent: dict
    error: Optional[str]
    online_agents: list[dict]
    selected_agents: list[dict]
    agent_results: list[dict]
    spatial_memory: dict
    verification_status: str
    verification_recommendation: str
    summary: str
    messages: list


# ---------------------------------------------------------------------------
# Parse prompt
# ---------------------------------------------------------------------------

_PARSE_PROMPT = (
    "You are a task planner for a multi-robot system.\n\n"
    "If the request is NOT about robot control, set is_valid_task=false.\n"
    "Otherwise, output exactly ONE phase.\n\n"
    "TASK TYPES:\n"
    "  direct_control — movement/posture/gesture, no detection\n"
    "  agent — camera detection + robot actions\n\n"
    "KEY FIELDS:\n"
    "  target_robot_ids: [] = all robots, ['id'] = specific robot\n"
    "  target_zone: 'A' = robots in zone A, '' = no filter\n"
    "  target_type: object, face, any, none\n"
    "  search_pattern: 'stationary' (stay in place) or 'patrol' (move around)\n"
    "  report_all: true = just observe/report, false = act on detection\n"
    "  on_detect_action: action after detection (e.g. 'wave'). Empty = just report.\n"
    "  duration_s: seconds if user specifies (e.g. 'for 5 seconds' → 5). 0 = default.\n\n"
    "RULES:\n"
    "1. 'person'/'people' → target_type='object'. 'face' → target_type='face'.\n"
    "2. report_all=true → default search_pattern='stationary'.\n"
    "   Only use 'patrol' if user explicitly says 'patrol and report'.\n"
    "3. on_detect_action: extract from user prompt. Leave empty if not specified.\n\n"
    "EXAMPLES:\n"
    "  'Wave' → direct_control\n"
    "  'robot-002 wave' → direct_control, target_robot_ids=['robot-002']\n"
    "  'Detect person, if found wave' → agent, target_type='object', on_detect_action='wave'\n"
    "  'Report what you see' → agent, target_type='any', report_all=true, search_pattern='stationary'\n"
    "  'Detect person for 10 seconds' → agent, target_type='object', duration_s=10"
)



def _build_parse_prompt(online_robots: list[dict]) -> str:
    """Build parse prompt, injecting capabilities and robot list."""
    all_actions = set()
    for robot in online_robots:
        caps = robot.get("capabilities")
        if not caps:
            continue
        for a in caps.get("actions", []):
            name = a if isinstance(a, str) else a.get("name", "")
            if name:
                all_actions.add(name)

    actions_str = ", ".join(sorted(all_actions)) if all_actions else ""
    actions_line = f"Supported actions: {actions_str}\n" if actions_str else ""

    robot_lines = []
    for robot in online_robots:
        rid = robot["id"]
        rtype = robot.get("robot_type", "spider")
        zone = robot.get("zone", "")
        zone_str = f", zone={zone}" if zone else ""
        busy_str = ", busy" if robot.get("busy") else ""
        robot_lines.append(f"  - {rid} (type={rtype}{zone_str}{busy_str})")
    robots_section = "\n".join(robot_lines) if robot_lines else "  (none)"

    return (
        f"{_PARSE_PROMPT}\n\n"
        f"AVAILABLE ROBOTS:\n{robots_section}\n\n"
        f"{actions_line}"
        "When the user refers to a robot by short name (e.g. '002', 'A'), "
        "match it to the closest robot ID from the list above."
    )


# ---------------------------------------------------------------------------
# Node: Parse + Get Online Robots
# ---------------------------------------------------------------------------

async def parse_and_discover(state: PlannerState) -> dict:
    """Parse intent (1 LLM call) and discover online robots via ADS/static."""
    messages_log = state.get("messages", [])

    # Discover online robots via ADS or static config
    discovery = AgentDiscovery()
    discovered = await discovery.get_online_agents()

    # Register discovered robots into registry for state tracking
    for agent_info in discovered:
        robot_registry.register_from_discovery(agent_info)

    # Build online_agents list — capabilities come from registry (via heartbeat),
    # not from discovery. ADS provides id/host/port/tags for selection,
    # heartbeat provides detailed capabilities for execution.
    online_agents = []
    for agent_info in discovered:
        # Skip offline robots
        if agent_info.get("status") == "offline":
            continue
        robot_id = agent_info["id"]
        reg_robot = robot_registry.get_robot(robot_id)
        caps = reg_robot.capabilities if reg_robot else {}
        zone = (reg_robot.zone if reg_robot else "") or agent_info.get("zone", "")
        robot_type = (reg_robot.robot_type if reg_robot else agent_info.get("robot_type", "spider"))
        online_agents.append({
            "id": robot_id,
            "host": agent_info["host"],
            "port": agent_info["port"],
            "camera_port": reg_robot.camera_port if reg_robot else agent_info.get("camera_port", 9000),
            "robot_type": robot_type,
            "capabilities": caps,
            "actions": [a["name"] for a in caps.get("actions", [])],
            "zone": zone,
            "status": "online",
            "busy": agent_info.get("busy", False),
        })

    if state.get("iteration", 1) > 1:
        # On replan, only refresh robot list
        if not online_agents:
            messages_log.append(AIMessage(content="Discovery: no robots online"))
            return {"online_agents": [], "error": "No robots are online", "messages": messages_log}
        messages_log.append(AIMessage(content=f"Discovery: {len(online_agents)} robots online"))
        return {"online_agents": online_agents, "messages": messages_log}

    prompt = state["prompt"]

    # All commands go through LLM for intelligent routing
    parse_prompt = _build_parse_prompt(online_agents)

    try:
        intent_llm = create_llm().with_structured_output(TaskIntent, strict=True)
        parse_result = await intent_llm.ainvoke([
            SystemMessage(content=parse_prompt),
            HumanMessage(content=prompt),
        ])
    except Exception as e:
        logger.warning(f"LLM parse failed: {e}, using fallback")
        parse_result = None

    if parse_result is None:
        intent = _fallback_intent(prompt)
        phase_descs = [f"{p['task_type']}:{p.get('target_type','')}" for p in intent['phases']]
        messages_log.append(AIMessage(content=f"Parse (fallback): [{', '.join(phase_descs)}]"))
    else:
        if not parse_result.is_valid_task:
            reason = parse_result.rejection_reason or "Not a robot task"
            logger.info(f"Parse rejected: {reason}")
            messages_log.append(AIMessage(content=f"Rejected: {reason}"))
            return {"error": f"NOT_VALID_TASK: {reason}", "messages": messages_log}
        intent = parse_result.model_dump()
        phase_descs = []
        for i, ph in enumerate(intent["phases"]):
            desc = f"{ph['task_type']}"
            if ph.get("target_robot_ids"):
                desc += f"→{ph['target_robot_ids']}"
            if ph.get("on_detect_action"):
                desc += f"(on_detect={ph['on_detect_action']})"
            phase_descs.append(desc)
        messages_log.append(AIMessage(content=f"Parse: {', '.join(phase_descs)}"))

        # Detailed intent for reasoning display
        ph = intent["phases"][0] if intent["phases"] else {}
        details = []
        if ph.get("task_type"): details.append(f"type={ph['task_type']}")
        if ph.get("target_type") and ph["target_type"] != "none": details.append(f"target={ph['target_type']}")
        if ph.get("target_name"): details.append(f"name='{ph['target_name']}'")
        if ph.get("search_pattern"): details.append(f"pattern={ph['search_pattern']}")
        if ph.get("target_zone"): details.append(f"zone={ph['target_zone']}")
        if ph.get("target_robot_ids"): details.append(f"robots={ph['target_robot_ids']}")
        if ph.get("report_all"): details.append("report_all=true")
        if ph.get("on_detect_action"): details.append(f"on_detect={ph['on_detect_action']}")
        if ph.get("on_detect_others_action") and ph["on_detect_others_action"] != "sit_down":
            details.append(f"others={ph['on_detect_others_action']}")
        if ph.get("duration_s", 0) > 0: details.append(f"duration={ph['duration_s']}s")
        if ph.get("description"): details.append(f"desc='{ph['description']}'")
        messages_log.append(AIMessage(content=f"Intent: {', '.join(details)}"))

        logger.info(f"Parsed intent: {intent}")

    if not online_agents:
        messages_log.append(AIMessage(content="Discovery: no robots online"))
        return {"intent": intent, "online_agents": [], "error": "No robots are online", "messages": messages_log}

    logger.info(f"Online robots: {[a['id'] for a in online_agents]}")
    messages_log.append(AIMessage(content=f"Discovery: {len(online_agents)} robots online"))
    return {"intent": intent, "online_agents": online_agents, "messages": messages_log}



def _fallback_intent(prompt: str) -> dict:
    """Fallback keyword-based intent classification."""
    p = prompt.lower()

    _DIRECT_KEYWORDS = [
        "forward", "go ahead", "move forward", "advance",
        "backward", "go back", "move back", "reverse",
        "turn left", "turn right", "rotate",
        "stand up", "get up", "rise",
        "sit down", "crouch", "lower",
        "stop", "halt", "freeze",
        "push up", "pushup",
        "wave", "say hi", "say hello", "greet",
        "look left", "look right", "look up", "look down",
        "dance", "dancing",
    ]

    is_direct = any(kw in p for kw in _DIRECT_KEYWORDS)
    is_detection = any(kw in p for kw in ["detect", "search", "find", "patrol", "scan", "look for"])

    if is_direct and not is_detection:
        return {
            "phases": [{"target_robot_ids": [], "task_type": "direct_control", "target_type": "none",
                        "search_pattern": "stationary", "action": "none", "description": prompt,
                        }],
            "urgency": "medium",
        }

    if any(kw in p for kw in ["face", "faces"]):
        target = "face"
    elif any(kw in p for kw in ["person", "people", "human"]):
        target = "object"
    elif any(kw in p for kw in ["red", "blue", "green", "color"]):
        target = "color"
    elif any(kw in p for kw in ["qr", "barcode", "code"]):
        target = "qr_code"
    else:
        target = "any"

    pattern = "stationary" if any(kw in p for kw in [
        "check here", "what do you see", "what you see", "report what",
        "look in front", "stay in place", "don't move", "stand still",
        "check what", "observe", "report",
    ]) else "patrol"
    is_report = any(kw in p for kw in ["report", "observe", "what do you see", "look around"])

    return {
        "phases": [{"target_robot_ids": [], "task_type": "agent", "target_type": target,
                    "search_pattern": pattern, "action": "none", "description": prompt,
                    "report_all": is_report, }],
        "urgency": "medium",
    }


# ---------------------------------------------------------------------------
# Helpers: resolve robots for a phase
# ---------------------------------------------------------------------------

def _resolve_phase_robots(phase: dict, online: list[dict]) -> list[dict]:
    """Resolve which robots a phase targets."""
    target_ids = phase.get("target_robot_ids", [])
    target_zone = phase.get("target_zone", "")

    if target_ids:
        id_set = set(target_ids)
        return [a for a in online if a["id"] in id_set]
    elif target_zone:
        zone_upper = target_zone.upper()
        return [a for a in online if a.get("zone", "").upper() == zone_upper]
    else:
        return list(online)


# ---------------------------------------------------------------------------
# Node: Execute Phases
# ---------------------------------------------------------------------------

async def execute_phases(state: PlannerState) -> dict:
    """Execute all phases sequentially, respecting triggers."""
    online = state.get("online_agents", [])
    intent = state.get("intent", {})
    prompt = state.get("prompt", "")
    messages_log = state.get("messages", [])

    if not online:
        return {"agent_results": [], "error": "No agents available", "messages": messages_log}

    phases = intent.get("phases", [])
    if not phases:
        phases = [{"target_robot_ids": [], "task_type": "agent", "target_type": "object",
                   "search_pattern": "patrol", "action": "none", "description": prompt}]

    # Always exactly one phase
    phase = phases[0]

    robots = _resolve_phase_robots(phase, online)
    if not robots:
        target_desc = phase.get("target_robot_ids") or phase.get("target_zone") or "any"
        return {"agent_results": [], "error": f"No robots matched ({target_desc})", "messages": messages_log}

    robot_ids = [r["id"] for r in robots]
    report_all = phase.get("report_all", False)
    mode = "report" if report_all else ("coordinated" if len(robots) > 1 and phase["task_type"] == "agent" else "independent")
    messages_log.append(AIMessage(
        content=f"Execute: {phase['task_type']} → {robot_ids} [{mode}]"
    ))
    logger.info(f"Execute: {phase['task_type']} → {robot_ids} mode={mode}")

    result = await _execute_single_phase(phase, robots, prompt, messages_log)
    agent_results = result.get("agent_results", [])

    if not agent_results:
        return {"agent_results": [], "error": "No results", "messages": messages_log}

    success_count = sum(1 for r in agent_results if r.get("status") == "success")
    messages_log.append(AIMessage(
        content=f"Done: {success_count}/{len(agent_results)} succeeded"
    ))

    error = "ALL_AGENTS_FAILED" if success_count == 0 else None
    return {"agent_results": agent_results, "error": error, "messages": messages_log}


async def _execute_single_phase(phase: dict, robots: list[dict], prompt: str, messages_log: list) -> dict:
    """Execute one phase across its target robots.

    Agent mode auto-coordination:
      - report_all=true  → parallel (each robot reports independently)
      - report_all=false → first_to_detect (first detection stops others)
    """
    task_type = phase.get("task_type", "direct_control")
    report_all = phase.get("report_all", False)

    if task_type == "agent" and not report_all and len(robots) > 1:
        return await _execute_coordinated_patrol(phase, robots, prompt, messages_log)

    if task_type == "agent":
        # Agent mode: dispatch to all target robots in parallel
        if len(robots) == 1:
            return await _dispatch_agent_task(robots[0], phase, prompt, messages_log)
        results = await asyncio.gather(*[
            _dispatch_agent_task(robot, phase, prompt, list(messages_log))
            for robot in robots
        ])
        # Merge agent_results from all dispatches
        merged = []
        for r in results:
            merged.extend(r.get("agent_results", []))
        return {"agent_results": merged}
    else:
        # Direct control: dispatch to all target robots in parallel
        description = phase.get("description", prompt)
        coros = [_dispatch_direct_control(robot, description) for robot in robots]
        raw_results = await asyncio.gather(*coros, return_exceptions=True)

        results = []
        for robot, result in zip(robots, raw_results):
            if isinstance(result, Exception):
                results.append({"agent_id": robot["id"], "status": "error",
                                "response": str(result), "structured": {}})
            else:
                results.append({
                    "agent_id": robot["id"],
                    "status": result.get("status", "error"),
                    "response": result.get("execution_result", {}).get("summary", ""),
                    "structured": result.get("execution_result", {}),
                })
        return {"agent_results": results}


async def _cancel_robot(robot_id: str):
    """Send cancel command to a Robot Agent via NATS."""
    from ..robot_agent import cancel_topic
    from ..infra.shared import ensure_connected
    try:
        transport = await ensure_connected()
        reply = await transport.request(
            cancel_topic(robot_id), json.dumps({"command": "cancel"}).encode(), timeout=5
        )
        logger.info(f"Cancel sent to {robot_id}: {json.loads(reply).get('status')}")
    except Exception as e:
        logger.warning(f"Cancel to {robot_id} failed: {e}")


async def _execute_coordinated_patrol(phase: dict, robots: list[dict], prompt: str, messages_log: list) -> dict:
    """Coordinated patrol with ReAct-style LLM coordination.

    LLM controls the coordination loop via tool calls:
    - dispatch_patrol: start patrol on robots
    - wait_for_detection: wait for next robot to report results
    - execute_action: make a robot perform an action
    - cancel_robot: stop a robot's patrol
    - finish: end the coordination

    The LLM sees tool outputs and decides what to do next,
    just like a ReAct agent with tool-calling.
    """
    import uuid

    if len(robots) < 2:
        return await _dispatch_agent_task(robots[0], phase, prompt, messages_log)

    # ── State shared between tools ──
    patrol_tasks: dict = {}       # robot_id → (asyncio.Task, robot_dict)
    detection_events: list = []    # [{robot_id, zone, detections, result}]
    agent_results: list = []       # final results to return
    completed: set = set()         # robot_ids that finished

    # ── Tool implementations ──
    async def _tool_dispatch_patrol(robot_ids: list[str]) -> str:
        """Start patrol on specified robots."""
        dispatched = []
        for rid in robot_ids:
            robot = next((r for r in robots if r["id"] == rid), None)
            if not robot or rid in patrol_tasks:
                continue
            task_payload = {
                "task_type": "agent",
                "mission_id": str(uuid.uuid4())[:8],
                "target_type": phase.get("target_type", "object"),
                "target_name": phase.get("target_name", ""),
                "search_pattern": phase.get("search_pattern", "patrol"),
                "report_all": phase.get("report_all", False),
                "task_goal": prompt,
            }
            coro = _dispatch_to_robot_agent(rid, task_payload, timeout=300)
            patrol_tasks[rid] = (asyncio.create_task(coro), robot)
            dispatched.append(rid)
        return f"Patrol started on: {dispatched}"

    async def _tool_wait_for_detection() -> str:
        """Wait for the next robot to complete its patrol. Returns detection results."""
        pending = set(t for rid, (t, _) in patrol_tasks.items() if rid not in completed)
        if not pending:
            return "No robots are patrolling."

        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        results_text = []
        for finished_task in done:
            for rid, (task, robot) in patrol_tasks.items():
                if task is finished_task:
                    try:
                        result = task.result()
                    except Exception as e:
                        result = {"status": "error", "error": str(e)}
                    completed.add(rid)

                    mission_result = result.get("mission_result", {})
                    dets = mission_result.get("detections", [])
                    zone = robot.get("zone", "?")

                    if mission_result.get("status") == "completed" and len(dets) > 0:
                        detection_events.append({
                            "robot_id": rid, "robot": robot,
                            "zone": zone, "detections": dets, "result": result,
                        })
                        labels = [f"{d.get('label', '?')}({d.get('confidence', 0):.0%})" for d in dets[:5]]
                        results_text.append(f"{rid} (zone {zone}): FOUND {', '.join(labels)}")
                    else:
                        status = mission_result.get("status", result.get("status", "?"))
                        results_text.append(f"{rid} (zone {zone}): {status}, no target detected")
                    break

        still_patrolling = [rid for rid in patrol_tasks if rid not in completed]
        summary = "\n".join(results_text)
        summary += f"\nStill patrolling: {still_patrolling if still_patrolling else 'none'}"
        return summary

    async def _tool_execute_action(robot_id: str, action: str) -> str:
        """Execute an action on a specific robot (e.g. wave, sit_down)."""
        robot = next((r for r in robots if r["id"] == robot_id), None)
        if not robot:
            return f"Robot {robot_id} not found"
        await _dispatch_direct_control(robot, action)
        return f"{robot_id}: executed {action}"

    async def _tool_cancel_robot(robot_id: str) -> str:
        """Cancel a robot's patrol mission."""
        await _cancel_robot(robot_id)
        completed.add(robot_id)
        # Also cancel the asyncio task
        if robot_id in patrol_tasks:
            task, _ = patrol_tasks[robot_id]
            task.cancel()
        return f"{robot_id}: cancelled"

    # ── Tool definitions for LLM ──
    tools = [
        {
            "type": "function",
            "function": {
                "name": "dispatch_patrol",
                "description": "Start patrol on specified robots. Call this first to begin the search mission.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "robot_ids": {"type": "array", "items": {"type": "string"},
                                      "description": "List of robot IDs to dispatch"}
                    },
                    "required": ["robot_ids"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "wait_for_detection",
                "description": "Wait for the next robot to complete and report results. Returns detection info or 'no target'.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
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
        },
        {
            "type": "function",
            "function": {
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
        },
        {
            "type": "function",
            "function": {
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
        },
    ]

    tool_map = {
        "dispatch_patrol": lambda args: _tool_dispatch_patrol(args.get("robot_ids", [])),
        "wait_for_detection": lambda args: _tool_wait_for_detection(),
        "execute_action": lambda args: _tool_execute_action(args["robot_id"], args["action"]),
        "cancel_robot": lambda args: _tool_cancel_robot(args["robot_id"]),
    }

    # ── ReAct loop ──
    robot_desc = ", ".join(f"{r['id']}(zone {r.get('zone', '?')})" for r in robots)
    system_msg = (
        "You are a multi-robot coordinator. You have tools to control robots.\n\n"
        f"Available robots: {robot_desc}\n\n"
        "Workflow:\n"
        "1. dispatch_patrol to start all robots searching\n"
        "2. wait_for_detection to see results as robots complete\n"
        "3. Based on the user's intent and detection results, decide what each robot should do next\n"
        "4. When the mission is complete, call finish\n\n"
        "You decide autonomously what actions to take based on the user's command.\n"
        "Consider what the user wants for EACH robot — the detecting robot AND the others.\n"
        "The user may want others to: sit down, stop, keep searching, go somewhere, or anything else.\n"
        "Read the user's command carefully and infer the right behavior.\n"
    )

    from openai import AsyncOpenAI
    import os

    # Parse model: "openai/gpt-4o-mini" → "gpt-4o-mini"
    from ..planner.llm_config import LLM_MODEL
    model = LLM_MODEL.split("/", 1)[-1] if "/" in LLM_MODEL else LLM_MODEL
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": f"User command: {prompt}"},
    ]

    MAX_TURNS = 15
    messages_log.append(AIMessage(content=f"ReAct coordination: {len(robots)} robots"))

    for turn in range(MAX_TURNS):
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            temperature=0.3,
        )

        choice = response.choices[0]
        msg = choice.message

        # Add assistant message to history
        messages.append(msg.model_dump(exclude_none=True))

        # No tool calls → LLM is done (or confused)
        if not msg.tool_calls:
            if msg.content:
                messages_log.append(AIMessage(content=f"Coordinator: {msg.content}"))
                logger.info(f"ReAct coordinator (turn {turn+1}): {msg.content}")
            break

        # Execute tool calls
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            logger.info(f"ReAct coordinator (turn {turn+1}): {fn_name}({fn_args})")
            messages_log.append(AIMessage(content=f"Tool: {fn_name}({fn_args})"))

            if fn_name == "finish":
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": fn_args.get("summary", "done"),
                })
                # Build final results
                for evt in detection_events:
                    zone_report = f"Target detected by {evt['robot_id']} in zone {evt['zone']}"
                    agent_results.append({
                        "agent_id": evt["robot_id"],
                        "status": "success",
                        "response": zone_report,
                        "structured": {
                            "mission": evt["result"].get("mission_result", {}),
                            "detections": evt["detections"],
                            "zone": evt["zone"],
                            "zone_report": zone_report,
                        },
                    })
                if not detection_events:
                    for robot in robots:
                        agent_results.append({
                            "agent_id": robot["id"],
                            "status": "success",
                            "response": "Patrol completed, no target detected",
                            "structured": {"detections": []},
                        })
                return {"agent_results": agent_results}

            # Execute the tool
            handler = tool_map.get(fn_name)
            if handler:
                output = await handler(fn_args)
            else:
                output = f"Unknown tool: {fn_name}"

            logger.info(f"ReAct tool output: {output[:200]}")
            messages_log.append(AIMessage(content=f"Result: {output[:200]}"))

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": output,
            })

    # Safety: if loop ended without finish, build results anyway
    logger.warning("ReAct coordination loop ended without finish call")
    for evt in detection_events:
        agent_results.append({
            "agent_id": evt["robot_id"],
            "status": "success",
            "response": f"Target detected in zone {evt['zone']}",
            "structured": {
                "mission": evt["result"].get("mission_result", {}),
                "detections": evt["detections"],
                "zone": evt["zone"],
            },
        })
    if not agent_results:
        for robot in robots:
            agent_results.append({
                "agent_id": robot["id"],
                "status": "success",
                "response": "Patrol completed",
                "structured": {},
            })
    return {"agent_results": agent_results}


async def _ping_robot(robot_id: str, timeout: float = 3) -> bool:
    """Quick check if Robot Agent is reachable via NATS."""
    from ..robot_agent import ping_topic
    from ..infra.shared import ensure_connected
    try:
        transport = await ensure_connected()
        reply = await transport.request(
            ping_topic(robot_id), b'{}', timeout=timeout
        )
        return True
    except Exception:
        return False


async def _dispatch_to_robot_agent(robot_id: str, task_payload: dict, timeout: float = 180) -> dict:
    """Send task to Robot Agent via NATS request/reply.

    Falls back to in-process execution if NATS fails.
    """
    from ..robot_agent import task_topic
    from ..infra.shared import ensure_connected

    # Quick ping to avoid long timeout on disconnected robots
    if not await _ping_robot(robot_id):
        logger.warning(f"Robot {robot_id} not reachable (ping failed), skipping")
        return {"status": "error", "error": f"Robot {robot_id} not reachable"}

    topic = task_topic(robot_id)
    payload_bytes = json.dumps(task_payload).encode()

    try:
        transport = await ensure_connected()
        reply_bytes = await transport.request(topic, payload_bytes, timeout=timeout)
        result = json.loads(reply_bytes)
        logger.info(f"NATS dispatch to {robot_id}: {result.get('status', '?')}")
        return result
    except Exception as e:
        logger.warning(f"NATS dispatch to {robot_id} failed ({e}), falling back to in-process")
        return await _fallback_in_process(robot_id, task_payload)


async def _fallback_in_process(robot_id: str, task_payload: dict) -> dict:
    """Fallback: execute task in-process (same as before refactor)."""
    import uuid
    from ..pi_agents.base_agent import pi_agent_manager
    from ..infra.robot_registry import robot_registry as reg

    task_type = task_payload.get("task_type", "direct_control")
    robot = reg.get_robot(robot_id)
    if not robot:
        return {"status": "error", "error": f"Robot {robot_id} not found in registry"}

    if task_type == "agent":
        from ..detection.engine import SearchMission, run_search_mission
        mission = SearchMission(
            mission_id=task_payload.get("mission_id", str(uuid.uuid4())[:8]),
            agent_id=robot_id,
            host=robot.host,
            camera_port=robot.camera_port,
            target_type=task_payload.get("target_type", "object"),
            target_name=task_payload.get("target_name", ""),
            task_goal=task_payload.get("task_goal", ""),
            search_pattern=task_payload.get("search_pattern", "patrol"),
            report_all=task_payload.get("report_all", False),
        )
        reg.set_busy(robot_id, mission.mission_id)
        result = await run_search_mission(mission)
        return {
            "status": "success" if result["status"] in ("completed", "timeout") else "error",
            "agent_id": robot_id,
            "mission_result": result,
        }
    else:
        pi_agent = pi_agent_manager.get_or_create(robot)
        prompt = task_payload.get("prompt", "")
        actions = task_payload.get("actions", [])
        if actions:
            result = await pi_agent.execute_steps([{"action": a} for a in actions])
        elif prompt:
            result = await pi_agent.execute_steps_from_prompt(prompt)
        else:
            return {"status": "error", "error": "No prompt or actions"}
        return {
            "status": "success" if result.get("mission_status") in ("completed", "partial") else "error",
            "agent_id": robot_id,
            "execution_result": result,
        }


async def _dispatch_agent_task(agent: dict, phase: dict, task_goal: str, messages_log: list) -> dict:
    """Dispatch agent mode task to Robot Agent via NATS."""
    import uuid

    task_payload = {
        "task_type": "agent",
        "mission_id": str(uuid.uuid4())[:8],
        "target_type": phase.get("target_type", "any"),
        "target_name": phase.get("target_name", ""),
        "search_pattern": phase.get("search_pattern", "patrol"),
        "report_all": phase.get("report_all", False),
        "task_goal": task_goal,
    }
    duration = phase.get("duration_s", 0)
    if duration > 0:
        task_payload["duration_s"] = duration

    target_desc = f"'{task_payload['target_name']}'" if task_payload['target_name'] else task_payload['target_type']
    mode_desc = "patrol" if task_payload['search_pattern'] != "stationary" else "stationary"
    messages_log.append(AIMessage(
        content=f"Agent: {mode_desc} on {agent['id']}, watching for {target_desc}"
    ))
    logger.info(f"Dispatching agent task to {agent['id']}: {task_payload['mission_id']} ({mode_desc})")

    result = await _dispatch_to_robot_agent(agent["id"], task_payload)

    mission_result = result.get("mission_result", {})
    agent_result = {
        "agent_id": agent["id"],
        "status": result.get("status", "error"),
        "response": (
            f"Agent {mission_result.get('status', '?')}: mode={mode_desc}, target={target_desc}, "
            f"frames={mission_result.get('frames_analyzed', 0)}, elapsed={mission_result.get('elapsed_s', 0)}s, "
            f"detections={len(mission_result.get('detections', []))}"
        ),
        "structured": {"mission": mission_result, "detections": mission_result.get("detections", [])},
    }

    if mission_result.get("match_result"):
        agent_result["response"] += f", match={mission_result['match_result']}"

    # Detailed agent result for reasoning
    det_count = len(mission_result.get("detections", []))
    elapsed = mission_result.get("elapsed_s", 0)
    frames = mission_result.get("frames_analyzed", 0)
    agent_status = mission_result.get("status", "unknown")
    detail = f"{agent['id']}: {agent_status}, {frames} frames, {elapsed:.0f}s, {det_count} detections"
    if mission_result.get("match_result"):
        match = mission_result["match_result"]
        detail += f", found {match.get('label', '?')} ({match.get('confidence', 0):.0%})"
    if mission_result.get("patrol_report", {}).get("summary"):
        detail += f" | Report: {mission_result['patrol_report']['summary'][:100]}"
    messages_log.append(AIMessage(content=f"Agent result: {detail}"))
    return {"agent_results": [agent_result], "messages": messages_log}


async def _dispatch_direct_control(agent_info: dict, prompt: str) -> dict:
    """Dispatch direct control task to Robot Agent via NATS."""
    task_payload = {"task_type": "direct_control", "prompt": prompt}
    return await _dispatch_to_robot_agent(agent_info["id"], task_payload, timeout=60)


# ---------------------------------------------------------------------------
# Nodes: Reflection, Check Status, Summarize, Handle Error
# ---------------------------------------------------------------------------

async def reflection(state: PlannerState) -> dict:
    results = state.get("agent_results", [])
    messages_log = state.get("messages", [])

    if not results or all(r.get("status") == "error" for r in results):
        messages_log.append(AIMessage(content="Reflection: all failed, replan"))
        return {"verification_status": "FAIL", "verification_recommendation": "replan", "messages": messages_log}

    messages_log.append(AIMessage(content="Reflection: success"))
    return {"verification_status": "PASS", "verification_recommendation": "success", "messages": messages_log}


async def check_status(state: PlannerState) -> dict:
    status = state.get("verification_status", "FAIL")
    iteration = state.get("iteration", 1)
    messages_log = state.get("messages", [])

    if status == "PASS":
        messages_log.append(AIMessage(content="Status: SUCCESS"))
    elif iteration >= MAX_REPLANS:
        messages_log.append(AIMessage(content=f"Status: MAX_REPLANS ({MAX_REPLANS}) reached"))
    else:
        messages_log.append(AIMessage(content=f"Status: REPLAN (iteration {iteration} -> {iteration + 1})"))

    return {"iteration": iteration + 1, "messages": messages_log}


async def summarize(state: PlannerState) -> dict:
    results = state.get("agent_results", [])
    messages_log = state.get("messages", [])
    iteration = state.get("iteration", 1)
    intent = state.get("intent", {})
    start_time = state.get("start_time", 0)
    total_latency = int((datetime.utcnow().timestamp() - start_time) * 1000) if start_time else 0

    if not results:
        return {"summary": "No results received from any agent."}

    success_results = [r for r in results if r.get("status") == "success"]
    error_results = [r for r in results if r.get("status") == "error"]

    if not success_results:
        error_msgs = "; ".join(r.get("error", "unknown") for r in error_results)
        return {"summary": f"All agents failed: {error_msgs}"}

    agents_text = []
    for r in success_results:
        agents_text.append(f"Agent {r.get('agent_id', 'unknown')}:\n{r.get('response', '')}")
    combined = "\n\n---\n\n".join(agents_text)

    summary = f"Completed: {len(success_results)} agent(s), {len(error_results)} failed.\n\n{combined}"
    summary += f"\n[Iterations: {iteration}, Total latency: {total_latency}ms]"
    messages_log.append(AIMessage(content=f"Summary: generated ({len(success_results)} agents)"))

    return {"summary": summary, "messages": messages_log}


async def handle_error(state: PlannerState) -> dict:
    error = state.get("error", "Unknown")
    messages_log = state.get("messages", [])

    if error.startswith("NOT_VALID_TASK:"):
        reason = error.split(":", 1)[1].strip()
        summary = f"This system is designed for robot patrol and search tasks only. {reason}"
    else:
        summary = f"Task failed: {error}"

    messages_log.append(AIMessage(content=f"Error handled: {error}"))
    return {"summary": summary, "messages": messages_log}


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------

def after_intent_guard(state: PlannerState) -> Literal["continue", "rejected"]:
    error = state.get("error", "")
    if error.startswith("NOT_VALID_TASK"):
        return "rejected"
    return "continue"


def after_execute(state: PlannerState) -> Literal["summarize", "reflect"]:
    results = state.get("agent_results", [])
    if results and any(r.get("status") == "success" for r in results):
        return "summarize"
    return "reflect"


def after_check_status(state: PlannerState) -> Literal["success", "replan", "max_replans", "error"]:
    if state.get("error") and not state.get("agent_results"):
        return "error"
    status = state.get("verification_status", "FAIL")
    iteration = state.get("iteration", 1)
    if status == "PASS":
        return "success"
    elif iteration > MAX_REPLANS:
        return "max_replans"
    else:
        return "replan"


# ---------------------------------------------------------------------------
# Build Graph
# ---------------------------------------------------------------------------

def build_planner_graph():
    graph = StateGraph(PlannerState)

    graph.add_node("parse_and_discover", parse_and_discover)
    graph.add_node("execute_phases", execute_phases)
    graph.add_node("reflection", reflection)
    graph.add_node("check_status", check_status)
    graph.add_node("summarize", summarize)
    graph.add_node("handle_error", handle_error)

    graph.add_edge(START, "parse_and_discover")
    graph.add_conditional_edges("parse_and_discover", after_intent_guard, {
        "continue": "execute_phases",
        "rejected": "handle_error",
    })
    graph.add_conditional_edges("execute_phases", after_execute, {
        "summarize": "summarize",
        "reflect": "reflection",
    })
    graph.add_edge("reflection", "check_status")
    graph.add_conditional_edges("check_status", after_check_status, {
        "success": "summarize",
        "replan": "parse_and_discover",
        "max_replans": "summarize",
        "error": "handle_error",
    })
    graph.add_edge("summarize", END)
    graph.add_edge("handle_error", END)

    return graph.compile()


planner_graph = build_planner_graph()


async def run_planner(
    prompt: str,
    strategy: str = "",
    agents: Optional[list[str]] = None,
) -> dict:
    initial_state: PlannerState = {
        "prompt": prompt,
        "strategy": strategy,
        "iteration": 1,
        "start_time": datetime.utcnow().timestamp(),
        "messages": [HumanMessage(content=prompt)],
    }
    return await planner_graph.ainvoke(initial_state)
