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
from ..detection.memory import detection_memory

logger = logging.getLogger(__name__)

MAX_REPLANS = 3


# ---------------------------------------------------------------------------
# Structured Outputs
# ---------------------------------------------------------------------------

class AgentSelection(BaseModel):
    selected_agent_ids: List[str] = Field(
        description="List of agent IDs selected, ordered by relevance"
    )
    reasoning: str = Field(description="Brief explanation of why these agents were selected")
    confidence: float = Field(description="Confidence score between 0.0 and 1.0")


class Step(BaseModel):
    task_type: str = Field(description="direct_control or agent")
    target_type: str = Field(default="none", description="face, color, qr_code, object, any, or none")
    search_pattern: str = Field(default="stationary", description="'stationary' or 'patrol'")
    action: str = Field(default="none", description="Control action or 'none'")
    description: str = Field(description="One-line description")
    target_name: str = Field(default="", description="For face search: specific person name")
    report_all: bool = Field(default=False, description="True if observe/report task")


class TaskIntent(BaseModel):
    is_valid_task: bool = Field(description="False if prompt is unrelated to robot control")
    rejection_reason: str = Field(default="", description="If is_valid_task=false, reason why")
    steps: List[Step] = Field(description="Task step (exactly one). Empty if is_valid_task=false")
    urgency: str = Field(description="low / medium / high")
    multi_agent: bool = Field(description="Whether the task requires multiple robots")
    target_robot_ids: List[str] = Field(
        default_factory=list,
        description="Specific robot IDs to use. Empty = auto-select based on multi_agent flag",
    )
    target_zone: str = Field(
        default="",
        description="Target zone letter (e.g. 'A', 'B'). Empty = no zone filter",
    )


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
    "You are a task planner for a multi-robot spider (PiCrawler) system.\n\n"
    "FIRST: check if the request is related to robot control. If NOT (e.g. 'write a poem',\n"
    "'what is the weather'), set is_valid_task=false with a rejection_reason and empty steps.\n\n"
    "If valid, classify into ONE step. There are TWO modes:\n\n"
    "MODE 1: EXECUTE MODE (task_type = direct_control)\n"
    "Pure movement / posture / gesture commands. No detection involved.\n"
    "Output EXACTLY ONE step. The robot's LLM will decompose composite commands.\n"
    "  action: 'none' (robot's LLM decides the actions)\n"
    "  target_type: none, search_pattern: stationary\n\n"
    "MODE 2: AGENT MODE (task_type = agent)\n"
    "Central watches camera and runs detection. The robot's AI decides actions.\n\n"
    "  target_type:\n"
    "    face - face recognition (only for 'detect faces' or 'find [name]')\n"
    "    object - YOLO object detection (for 'detect person/people/cat/car/...')\n"
    "  search_pattern:\n"
    "    stationary - robot stays in place, only checks current camera view\n"
    "               ONLY use when user explicitly says 'check here', 'look in front', 'what do you see', 'stay in place', 'don't move', 'stand still'\n"
    "    patrol     - robot moves around to search the area (DEFAULT for all detection/search tasks)\n"
    "  target_name: (optional, for finding a specific person)\n\n"
    "CRITICAL RULES:\n"
    "1. NEVER mix direct_control with agent.\n"
    "2. Conditional logic ('if detected, wave') is VALID — classify as agent mode. The condition is handled at runtime.\n"
    "3. ALWAYS output exactly ONE step.\n"
    "4. report_all: true for observe/report only, false for conditional actions.\n"
    "5. 'person'/'people' -> target_type: 'object' (YOLO), NOT 'face'.\n"
    "   'face'/'faces' -> target_type: 'face' (face recognition).\n\n"
    "EXAMPLES:\n"
    "  Rejected: 'Write me a poem' -> is_valid_task=false, steps=[]\n"
    "  Execute: 'Wave then dance' -> steps=[{task_type: 'direct_control', action: 'none'}]\n"
    "  Agent: 'Detect person, if found wave' -> steps=[{task_type: 'agent', target_type: 'object', search_pattern: 'patrol', report_all: false}]\n"
    "  Agent: 'Check what is in front of you' -> steps=[{task_type: 'agent', target_type: 'any', search_pattern: 'stationary', report_all: true}]\n"
    "  Agent: 'Find John' -> steps=[{task_type: 'agent', target_type: 'face', target_name: 'John', search_pattern: 'patrol'}]"
)


# ---------------------------------------------------------------------------
# Fast path: skip Planner LLM for obvious simple commands
# ---------------------------------------------------------------------------

# Single-action keywords that map directly to direct_control
_FAST_DIRECT_MAP = {
    "stand up": "stand_up", "get up": "stand_up", "rise": "stand_up",
    "sit down": "sit_down", "sit": "sit_down",
    "stop": "stop", "halt": "stop", "freeze": "stop",
    "forward": "forward", "move forward": "forward", "go forward": "forward",
    "go ahead": "forward", "advance": "forward",
    "backward": "backward", "go back": "backward", "move back": "backward",
    "back up": "backward", "reverse": "backward",
    "turn left": "turn_left", "turn right": "turn_right",
    "look left": "look_left", "look right": "look_right",
    "look up": "look_up", "look down": "look_down",
    "wave": "wave", "say hi": "wave", "say hello": "wave", "greet": "wave",
    "dance": "dance", "dancing": "dance",
    "push up": "push_up", "pushup": "push_up", "push ups": "push_up",
    "do push ups": "push_up", "do pushups": "push_up",
    "nod": "nod", "shake head": "shake_head",
    "shake hand": "shake_hand", "handshake": "shake_hand",
    "play dead": "play_dead",
}

# Keywords that indicate detection/agent mode — never fast-path these
_AGENT_KEYWORDS = {"detect", "search", "find", "patrol", "scan", "look for",
                   "if found", "if detect", "otherwise", "report", "observe"}


def _try_fast_classify(prompt: str) -> dict | None:
    """Try to classify simple commands without LLM. Returns intent dict or None."""
    p = prompt.lower().strip().rstrip(".!?")

    # If any agent keyword present, must go through LLM
    if any(kw in p for kw in _AGENT_KEYWORDS):
        return None

    # Check exact or near-exact match
    if p in _FAST_DIRECT_MAP:
        return {
            "is_valid_task": True,
            "steps": [{"task_type": "direct_control", "target_type": "none",
                       "search_pattern": "stationary", "action": "none",
                       "description": prompt, "target_name": "", "report_all": False}],
            "urgency": "medium", "multi_agent": False,
        }

    # Check if prompt is a simple combo of known actions (e.g. "wave then dance", "stand up and wave")
    # Split by common conjunctions
    for sep in [" then ", " and then ", " and ", ", "]:
        if sep in p:
            parts = [part.strip() for part in p.split(sep)]
            if all(part in _FAST_DIRECT_MAP for part in parts):
                return {
                    "is_valid_task": True,
                    "steps": [{"task_type": "direct_control", "target_type": "none",
                               "search_pattern": "stationary", "action": "none",
                               "description": prompt, "target_name": "", "report_all": False}],
                    "urgency": "medium", "multi_agent": False,
                }

    return None


def _try_fast_decompose(prompt: str) -> list | None:
    """Try to decompose simple commands into action list without Brain LLM.
    Returns list of action dicts or None if LLM is needed."""
    p = prompt.lower().strip().rstrip(".!?")

    # If any agent keyword present, can't fast decompose
    if any(kw in p for kw in _AGENT_KEYWORDS):
        return None

    # Single action
    if p in _FAST_DIRECT_MAP:
        action = _FAST_DIRECT_MAP[p]
        actions = []
        # Auto-prepend stand_up for actions that require standing
        if action not in ("stand_up", "sit_down", "stop"):
            actions.append({"action": "stand_up"})
        actions.append({"action": action})
        return actions

    # Multi-action combo (e.g. "wave then dance")
    for sep in [" then ", " and then ", " and ", ", "]:
        if sep in p:
            parts = [part.strip() for part in p.split(sep)]
            if all(part in _FAST_DIRECT_MAP for part in parts):
                mapped = [_FAST_DIRECT_MAP[part] for part in parts]
                actions = []
                # Auto-prepend stand_up if first action needs it
                if mapped[0] not in ("stand_up", "sit_down", "stop"):
                    actions.append({"action": "stand_up"})
                for a in mapped:
                    actions.append({"action": a})
                return actions

    return None


def _build_parse_prompt(online_robots: list[dict]) -> str:
    """Build parse prompt, injecting capabilities and robot list for scope decisions."""
    all_actions = set()
    has_caps = False

    for robot in online_robots:
        caps = robot.get("capabilities")
        if not caps:
            continue
        has_caps = True
        for a in caps.get("actions", []):
            name = a if isinstance(a, str) else a.get("name", "")
            if name:
                all_actions.add(name)

    actions_str = ", ".join(sorted(all_actions)) if all_actions else ""
    actions_line = f"  Supported actions: {actions_str}\n" if actions_str else ""

    # Build robot list for scope decisions
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
        "You are a task planner for a multi-robot system.\n"
        "Robot types: spider (patrol+detect), turtlebot (lidar+localize), car (execute tasks).\n\n"
        "FIRST: check if the request is related to robot control. If NOT,\n"
        "set is_valid_task=false with a rejection_reason and empty steps.\n\n"
        f"AVAILABLE ROBOTS:\n{robots_section}\n\n"
        "If valid, classify into ONE step. There are TWO modes:\n\n"
        "MODE 1: EXECUTE MODE (task_type = direct_control)\n"
        "Pure movement / posture / gesture commands. No detection involved.\n"
        f"{actions_line}"
        "  action: 'none' (robot's LLM decides the actions)\n"
        "  target_type: none, search_pattern: stationary\n\n"
        "MODE 2: AGENT MODE (task_type = agent)\n"
        "Central watches camera and runs detection. The robot's AI decides actions.\n\n"
        "  target_type: face, object, color, qr_code, any\n"
        "  search_pattern:\n"
        "    stationary - robot stays in place, only checks current camera view (short timeout)\n"
        "               ONLY use when user explicitly says 'check here', 'look in front', 'what do you see', 'stay in place', 'don't move', 'stand still'\n"
        "    patrol     - robot moves around to search the area (DEFAULT for all detection/search tasks)\n"
        "  target_name: (optional, for finding a specific person)\n\n"
        "SCOPE RULES (target_robot_ids, target_zone, multi_agent):\n"
        "- If user names a specific robot → target_robot_ids=[that id], multi_agent=false\n"
        "- If user mentions a zone (e.g. 'zone A') → target_zone='A'\n"
        "- If user says 'all robots'/'both'/'everyone' → multi_agent=true, target_robot_ids=[]\n"
        "- Detection/search tasks with no specific robot → multi_agent=true (more eyes = faster)\n"
        "- Direct control with no specific robot → multi_agent=false (LLM picks best)\n"
        "- target_robot_ids=[] means auto-select based on multi_agent and target_zone\n\n"
        "CRITICAL RULES:\n"
        "1. NEVER mix direct_control with agent.\n"
        "2. Conditional logic ('if detected, wave') is VALID — classify as agent mode. The condition is handled at runtime.\n"
        "3. ALWAYS output exactly ONE step.\n"
        "4. report_all: true for observe/report only, false for conditional actions.\n"
        "5. 'person'/'people' -> target_type: 'object' (YOLO), NOT 'face'.\n"
        "   'face'/'faces' -> target_type: 'face' (face recognition).\n\n"
        "EXAMPLES:\n"
        "  Rejected: 'Write me a poem' -> is_valid_task=false, steps=[]\n"
        "  Execute: 'Wave then dance' -> multi_agent=false, target_robot_ids=[], steps=[{task_type: 'direct_control'}]\n"
        "  Execute: 'crawler-001 stand up' -> multi_agent=false, target_robot_ids=['crawler-001'], steps=[{task_type: 'direct_control'}]\n"
        "  Agent: 'Detect person, if found wave' -> multi_agent=true, steps=[{task_type: 'agent', target_type: 'object', search_pattern: 'patrol'}]\n"
        "  Agent: 'Zone A detect person' -> multi_agent=true, target_zone='A', steps=[{task_type: 'agent', target_type: 'object', search_pattern: 'patrol'}]\n"
        "  Agent: 'Check what is in front' -> multi_agent=false, steps=[{task_type: 'agent', target_type: 'any', search_pattern: 'stationary', report_all: true}]\n"
        "  Agent: 'Find John' -> multi_agent=true, steps=[{task_type: 'agent', target_type: 'face', target_name: 'John', search_pattern: 'patrol'}]"
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

    # ── Fast path: skip Planner LLM for simple direct commands ──
    fast_intent = _try_fast_classify(prompt)
    if fast_intent is not None:
        logger.info(f"Fast path: '{prompt}' -> direct_control (skipped Planner LLM)")
        messages_log.append(AIMessage(content="Parse (fast): direct_control"))
        intent = fast_intent
    else:
        # Full LLM call: guard + parse
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
            step_descs = [f"{s['task_type']}:{s.get('action','') or s.get('target_type','')}" for s in intent['steps']]
            messages_log.append(AIMessage(content=f"Parse (fallback): [{', '.join(step_descs)}]"))
        else:
            if not parse_result.is_valid_task:
                reason = parse_result.rejection_reason or "Not a robot task"
                logger.info(f"Parse rejected: {reason}")
                messages_log.append(AIMessage(content=f"Rejected: {reason}"))
                return {"error": f"NOT_VALID_TASK: {reason}", "messages": messages_log}
            intent = parse_result.model_dump()
            # LLM now handles scope directly via target_robot_ids / target_zone / multi_agent
            scope_desc = []
            if intent.get("target_robot_ids"):
                scope_desc.append(f"robots={intent['target_robot_ids']}")
            if intent.get("target_zone"):
                scope_desc.append(f"zone={intent['target_zone']}")
            scope_desc.append(f"multi={intent['multi_agent']}")
            step_descs = [f"{s['task_type']}:{s.get('action','') or s.get('target_type','')}" for s in intent['steps']]
            messages_log.append(AIMessage(content=f"Parse: {', '.join(step_descs)}, {', '.join(scope_desc)}"))
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
            "steps": [{"task_type": "direct_control", "target_type": "none",
                       "search_pattern": "none", "action": "none", "description": prompt}],
            "urgency": "medium", "multi_agent": False,
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

    # Default: patrol. Stationary only for explicit "check here" / "what do you see" type prompts
    pattern = "stationary" if any(kw in p for kw in ["check here", "what do you see", "look in front", "stay in place", "don't move", "stand still"]) else "patrol"
    is_report = any(kw in p for kw in ["report", "observe", "what do you see", "look around"])

    return {
        "steps": [{"task_type": "agent", "target_type": target, "search_pattern": pattern,
                   "action": "none", "description": prompt, "report_all": is_report}],
        "urgency": "medium", "multi_agent": True,
    }


# ---------------------------------------------------------------------------
# Node: Route Task
# ---------------------------------------------------------------------------

async def route_task(state: PlannerState) -> dict:
    online = state.get("online_agents", [])
    intent = state.get("intent", {})
    strategy = state.get("strategy", "")
    iteration = state.get("iteration", 1)
    messages_log = state.get("messages", [])

    if not online:
        return {"selected_agents": [], "error": "No agents available", "messages": messages_log}

    # Scope resolution: LLM already decided target_robot_ids / target_zone / multi_agent
    target_robot_ids = intent.get("target_robot_ids", [])
    target_zone = intent.get("target_zone", "")

    if target_robot_ids:
        # LLM picked specific robots
        id_set = set(target_robot_ids)
        selected = [a for a in online if a["id"] in id_set]
        if not selected:
            return {"selected_agents": [], "error": f"Robots {target_robot_ids} not online", "messages": messages_log}
        strategy = "parallel" if len(selected) > 1 else "single"
        messages_log.append(AIMessage(content=f"Target robots: {[a['id'] for a in selected]}"))
    elif target_zone:
        # LLM picked a zone — filter online robots by zone
        zone_upper = target_zone.upper()
        selected = [a for a in online if a.get("zone", "").upper() == zone_upper]
        if not selected:
            return {"selected_agents": [], "error": f"No robots in zone {target_zone}", "messages": messages_log}
        strategy = "parallel" if len(selected) > 1 else "single"
        messages_log.append(AIMessage(content=f"Zone {target_zone} robots: {[a['id'] for a in selected]}"))
    else:
        selected = online

        steps_list = intent.get("steps", [])
        has_direct_control = any(s.get("task_type") == "direct_control" for s in steps_list)

        if not strategy:
            if intent.get("multi_agent") and len(selected) > 1:
                strategy = "parallel"
            elif has_direct_control:
                strategy = "single"
            elif iteration > 1:
                strategy = "parallel"
            else:
                strategy = "single"

        if strategy == "single":
            selected = [selected[0]]

    messages_log.append(AIMessage(content=f"Route: {strategy}, {len(selected)} robot(s)"))
    return {"selected_agents": selected, "strategy": strategy, "messages": messages_log}


def _build_agent_catalog(agents: list[dict]) -> str:
    from ..infra.robot_registry import robot_registry
    lines = []
    for agent in agents:
        reg = robot_registry.get_robot(agent["id"])
        robot_type = (reg.robot_type if reg else agent.get("robot_type", "spider"))
        line = f"- robot_id: {agent['id']}\n"
        line += f"  type: {robot_type}\n"
        line += f"  host: {agent['host']}\n"
        if reg and reg.zone:
            line += f"  zone: {reg.zone}\n"
        line += f"  status: {agent.get('status', 'online')}\n"
        line += f"  busy: {agent.get('busy', False)}\n"
        actions = agent.get("actions", [])
        if actions:
            line += f"  actions: {', '.join(actions[:10])}\n"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node: Execute Task
# ---------------------------------------------------------------------------

async def execute_task(state: PlannerState) -> dict:
    selected = state.get("selected_agents", [])
    intent = state.get("intent", {})
    prompt = state.get("prompt", "")
    messages_log = state.get("messages", [])

    if not selected:
        return {"agent_results": [], "error": "No agents selected", "messages": messages_log}

    steps = intent.get("steps", [])
    if not steps:
        steps = [{"task_type": "agent", "target_type": "object", "search_pattern": "patrol",
                  "action": "none", "description": prompt}]

    step_types = {s.get("task_type") for s in steps}

    # Agent Mode — dispatch via NATS to Robot Agent
    if "agent" in step_types:
        if len(selected) == 1:
            return await _dispatch_agent_task(selected[0], steps, prompt, messages_log)
        results = await asyncio.gather(*[
            _dispatch_agent_task(agent, steps, prompt, list(messages_log))
            for agent in selected
        ])
        for r in results:
            if r.get("agent_results", [{}])[0].get("status") == "success":
                return r
        return results[0]

    # Direct Control Mode — dispatch via NATS to Robot Agent
    messages_log.append(AIMessage(content=f"Execute: direct_control '{prompt}'"))
    fast_actions = _try_fast_decompose(prompt)
    strategy = state.get("strategy", "parallel")

    if strategy == "sequential":
        for agent_info in selected:
            result = await _dispatch_direct_control(agent_info, prompt, fast_actions)
            agent_result = {
                "agent_id": agent_info["id"],
                "status": result.get("status", "error"),
                "response": result.get("execution_result", {}).get("summary", ""),
                "structured": result.get("execution_result", {}),
            }
            if agent_result["status"] == "success":
                messages_log.append(AIMessage(content=f"Execute: {agent_info['id']} succeeded"))
                return {"agent_results": [agent_result], "messages": messages_log}
        return {"agent_results": [], "error": "All sequential agents failed", "messages": messages_log}
    else:
        coros = [
            _dispatch_direct_control(agent_info, prompt, fast_actions)
            for agent_info in selected
        ]
        raw_results = await asyncio.gather(*coros, return_exceptions=True)

        results = []
        for agent_info, result in zip(selected, raw_results):
            if isinstance(result, Exception):
                results.append({"agent_id": agent_info["id"], "status": "error", "response": str(result), "structured": {}})
            else:
                results.append({
                    "agent_id": agent_info["id"],
                    "status": result.get("status", "error"),
                    "response": result.get("execution_result", {}).get("summary", ""),
                    "structured": result.get("execution_result", {}),
                })

        success_count = sum(1 for r in results if r.get("status") == "success")
        messages_log.append(AIMessage(content=f"Execute: {success_count}/{len(results)} succeeded"))
        if success_count == 0:
            return {"agent_results": results, "error": "ALL_AGENTS_FAILED", "messages": messages_log}
        return {"agent_results": results, "messages": messages_log}


async def _dispatch_to_robot_agent(robot_id: str, task_payload: dict, timeout: float = 180) -> dict:
    """Send task to Robot Agent via NATS request/reply.

    Falls back to in-process execution if NATS fails.
    """
    from ..robot_agent import task_topic
    from ..infra.shared import ensure_connected

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


async def _dispatch_agent_task(agent: dict, steps: list[dict], task_goal: str, messages_log: list) -> dict:
    """Dispatch agent mode task to Robot Agent via NATS."""
    import uuid
    agent_step = next((s for s in steps if s.get("task_type") == "agent"), steps[0])

    task_payload = {
        "task_type": "agent",
        "mission_id": str(uuid.uuid4())[:8],
        "target_type": agent_step.get("target_type", "any"),
        "target_name": agent_step.get("target_name", ""),
        "search_pattern": agent_step.get("search_pattern", "patrol"),
        "report_all": agent_step.get("report_all", False),
        "task_goal": task_goal,
    }

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

    messages_log.append(AIMessage(content=f"Agent: {mission_result.get('status', 'unknown')}"))
    return {"agent_results": [agent_result], "messages": messages_log}


async def _dispatch_direct_control(agent_info: dict, prompt: str, fast_actions: list = None) -> dict:
    """Dispatch direct control task to Robot Agent via NATS."""
    task_payload = {"task_type": "direct_control", "prompt": prompt}
    if fast_actions:
        task_payload["actions"] = [a["action"] if isinstance(a, dict) else a for a in fast_actions]
        logger.info(f"Fast decompose: '{prompt}' -> {task_payload['actions']} (skipped Brain LLM)")

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

    # Record to memory
    try:
        steps = intent.get("steps", [])
        task_type = steps[0].get("task_type", "unknown") if steps else "unknown"
        target_type = steps[0].get("target_type", "none") if steps else "none"

        for r in success_results:
            structured = r.get("structured", {})
            detections = structured.get("detections", [])
            if detections:
                detection_memory.record_detections(
                    agent_id=r.get("agent_id", "unknown"),
                    task_type=task_type,
                    target_type=target_type,
                    detections=detections,
                )
            detection_memory.record_mission(
                task_id=f"iter-{iteration}",
                agent_id=r.get("agent_id", "unknown"),
                prompt=state.get("prompt", ""),
                task_type=task_type,
                status="success",
                duration_ms=total_latency,
                detection_count=len(detections),
                summary=summary[:500],
            )
    except Exception as e:
        logger.warning(f"Memory recording failed: {e}")

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
    graph.add_node("route_task", route_task)
    graph.add_node("execute_task", execute_task)
    graph.add_node("reflection", reflection)
    graph.add_node("check_status", check_status)
    graph.add_node("summarize", summarize)
    graph.add_node("handle_error", handle_error)

    graph.add_edge(START, "parse_and_discover")
    graph.add_conditional_edges("parse_and_discover", after_intent_guard, {
        "continue": "route_task",
        "rejected": "handle_error",
    })
    graph.add_edge("route_task", "execute_task")
    graph.add_conditional_edges("execute_task", after_execute, {
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
