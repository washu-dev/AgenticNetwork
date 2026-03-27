"""
LangGraph-based Planner for PiCrawler Multi-Agent System.

Adapted for Command Center architecture:
- Uses AgentDiscovery (ADS / static) to find robots
- Registers discovered robots in robot_registry for state tracking
- Dispatches to CentralPiAgent instead of A2A client
- All LLM decisions happen locally
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
    multi_agent: bool = Field(description="Whether the task benefits from multiple robots")


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
    "    stationary - robot stays in place\n"
    "    patrol - robot moves while detecting\n"
    "  target_name: (optional, for finding a specific person)\n\n"
    "CRITICAL RULES:\n"
    "1. NEVER mix direct_control with agent.\n"
    "2. Conditional logic (if/then/else) is NOT parsed - forwarded as task_goal.\n"
    "3. ALWAYS output exactly ONE step.\n"
    "4. report_all: true for observe/report only, false for conditional actions.\n"
    "5. 'person'/'people' -> target_type: 'object' (YOLO), NOT 'face'.\n"
    "   'face'/'faces' -> target_type: 'face' (face recognition).\n\n"
    "EXAMPLES:\n"
    "  Rejected: 'Write me a poem' -> is_valid_task=false, steps=[]\n"
    "  Execute: 'Wave then dance' -> steps=[{task_type: 'direct_control', action: 'none'}]\n"
    "  Agent: 'Detect person, if found wave' -> steps=[{task_type: 'agent', target_type: 'object', search_pattern: 'stationary', report_all: false}]\n"
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
    """Build parse prompt, injecting capabilities from registered robots."""
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

    if not has_caps:
        return _PARSE_PROMPT

    actions_str = ", ".join(sorted(all_actions))
    logger.info(f"Building parse prompt with capabilities: actions=[{actions_str}]")

    return (
        "You are a task planner for a multi-robot spider (PiCrawler) system.\n\n"
        "FIRST: check if the request is related to robot control. If NOT,\n"
        "set is_valid_task=false with a rejection_reason and empty steps.\n\n"
        "If valid, classify into ONE step. There are TWO modes:\n\n"
        "MODE 1: EXECUTE MODE (task_type = direct_control)\n"
        "Pure movement / posture / gesture commands. No detection involved.\n"
        f"  Supported actions: {actions_str}\n"
        "  action: 'none' (robot's LLM decides the actions)\n"
        "  target_type: none, search_pattern: stationary\n\n"
        "MODE 2: AGENT MODE (task_type = agent)\n"
        "Central watches camera and runs detection. The robot's AI decides actions.\n\n"
        "  target_type: face, object, color, qr_code, any\n"
        "  search_pattern: stationary or patrol\n"
        "  target_name: (optional, for finding a specific person)\n\n"
        "CRITICAL RULES:\n"
        "1. NEVER mix direct_control with agent.\n"
        "2. Conditional logic (if/then/else) is NOT parsed - forwarded as task_goal.\n"
        "3. ALWAYS output exactly ONE step.\n"
        "4. report_all: true for observe/report only, false for conditional actions.\n"
        "5. 'person'/'people' -> target_type: 'object' (YOLO), NOT 'face'.\n"
        "   'face'/'faces' -> target_type: 'face' (face recognition).\n\n"
        "EXAMPLES:\n"
        "  Rejected: 'Write me a poem' -> is_valid_task=false, steps=[]\n"
        "  Execute: 'Wave then dance' -> steps=[{task_type: 'direct_control', action: 'none'}]\n"
        "  Agent: 'Detect person, if found wave' -> steps=[{task_type: 'agent', target_type: 'object', search_pattern: 'stationary', report_all: false}]\n"
        "  Agent: 'Find John' -> steps=[{task_type: 'agent', target_type: 'face', target_name: 'John', search_pattern: 'patrol'}]"
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

    online_agents = [
        {
            "id": agent_info["id"],
            "host": agent_info["host"],
            "port": agent_info["port"],
            "camera_port": agent_info.get("camera_port", 9000),
            "capabilities": agent_info.get("capabilities") or {},
            "actions": [a["name"] for a in (agent_info.get("capabilities") or {}).get("actions", [])],
            "status": "online",
            "busy": agent_info.get("busy", False),
        }
        for agent_info in discovered
    ]

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
            step_descs = [f"{s['task_type']}:{s.get('action','') or s.get('target_type','')}" for s in intent['steps']]
            messages_log.append(AIMessage(content=f"Parse: {', '.join(step_descs)}, multi={intent['multi_agent']}"))
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

    pattern = "stationary" if any(kw in p for kw in ["detect", "look", "observe"]) else "patrol"
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

    selected = online
    if len(online) > 1:
        try:
            catalog = _build_agent_catalog(online)
            if catalog:
                selection_llm = create_llm().with_structured_output(AgentSelection, strict=True)
                sys_msg = SystemMessage(content=f"""You are an intelligent agent router for a PiCrawler robot fleet.
Select the most suitable robot(s) for the task.

Available Robots:
{catalog}

Select robots most relevant to the task. Order by relevance.""")
                user_msg = HumanMessage(content=f"Task: {state['prompt']}\nTarget: {intent.get('target_type', 'unknown')}")
                result = await selection_llm.ainvoke([sys_msg, user_msg])

                selected_ids = set(result.selected_agent_ids)
                llm_selected = [a for a in online if a["id"] in selected_ids]
                if llm_selected:
                    selected = llm_selected
                    messages_log.append(AIMessage(content=f"Selected {len(selected)} robots: {[a['id'] for a in selected]}"))
        except Exception as e:
            logger.warning(f"LLM agent selection failed: {e}, using all")

    steps_list = intent.get("steps", [])
    has_direct_control = any(s.get("task_type") == "direct_control" for s in steps_list)

    if not strategy:
        if has_direct_control:
            strategy = "single"
        elif iteration > 1:
            strategy = "parallel"
        elif intent.get("multi_agent", True) and len(selected) > 1:
            strategy = "parallel"
        else:
            strategy = "single"

    if strategy == "single":
        selected = [selected[0]]

    messages_log.append(AIMessage(content=f"Route: {strategy}, {len(selected)} robot(s)"))
    return {"selected_agents": selected, "strategy": strategy, "messages": messages_log}


def _build_agent_catalog(agents: list[dict]) -> str:
    lines = []
    for agent in agents:
        caps = agent.get("capabilities", {})
        line = f"- robot_id: {agent['id']}\n"
        line += f"  host: {agent['host']}\n"
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

    # Agent Mode
    if "agent" in step_types:
        return await _execute_agent(selected[0], steps, prompt, messages_log)

    # Direct Control Mode
    from ..pi_agents.base_agent import pi_agent_manager
    from ..infra.robot_registry import robot_registry as reg

    messages_log.append(AIMessage(content=f"Execute: direct_control '{prompt}'"))

    # Fast path: if prompt maps to known actions, skip Brain LLM decompose
    fast_actions = _try_fast_decompose(prompt)

    strategy = state.get("strategy", "parallel")

    if strategy == "sequential":
        for agent_info in selected:
            robot = reg.get_robot(agent_info["id"])
            if not robot:
                continue
            pi_agent = pi_agent_manager.get_or_create(robot)
            if fast_actions:
                logger.info(f"Fast decompose: '{prompt}' -> {fast_actions} (skipped Brain LLM)")
                result = await pi_agent.execute_steps(fast_actions)
            else:
                result = await pi_agent.execute_steps_from_prompt(prompt)
            agent_result = {
                "agent_id": agent_info["id"],
                "status": "success" if result.get("mission_status") in ("completed", "partial") else "error",
                "response": result.get("summary", ""),
                "structured": result,
            }
            if agent_result["status"] == "success":
                messages_log.append(AIMessage(content=f"Execute: {agent_info['id']} succeeded"))
                return {"agent_results": [agent_result], "messages": messages_log}
        return {"agent_results": [], "error": "All sequential agents failed", "messages": messages_log}
    else:
        tasks = []
        for agent_info in selected:
            robot = reg.get_robot(agent_info["id"])
            if not robot:
                continue
            pi_agent = pi_agent_manager.get_or_create(robot)
            if fast_actions:
                logger.info(f"Fast decompose: '{prompt}' -> {fast_actions} (skipped Brain LLM)")
                tasks.append((agent_info["id"], pi_agent.execute_steps(fast_actions)))
            else:
                tasks.append((agent_info["id"], pi_agent.execute_steps_from_prompt(prompt)))

        results = []
        for agent_id, coro in tasks:
            result = await coro
            results.append({
                "agent_id": agent_id,
                "status": "success" if result.get("mission_status") in ("completed", "partial") else "error",
                "response": result.get("summary", ""),
                "structured": result,
            })

        success_count = sum(1 for r in results if r.get("status") == "success")
        messages_log.append(AIMessage(content=f"Execute: {success_count}/{len(results)} succeeded"))
        if success_count == 0:
            return {"agent_results": results, "error": "ALL_AGENTS_FAILED", "messages": messages_log}
        return {"agent_results": results, "messages": messages_log}


async def _execute_agent(agent: dict, steps: list[dict], task_goal: str, messages_log: list) -> dict:
    """Agent Mode: Central detects + CentralPiAgent decides actions."""
    import uuid
    from ..detection.engine import SearchMission, run_search_mission
    from ..infra.robot_registry import robot_registry as reg

    agent_step = next((s for s in steps if s.get("task_type") == "agent"), steps[0])
    target_type = agent_step.get("target_type", "any")
    target_name = agent_step.get("target_name", "")
    search_pattern = agent_step.get("search_pattern", "stationary")
    report_all = agent_step.get("report_all", False)

    # camera_port: try from agent info, then from registry
    camera_port = agent.get("camera_port", 9000)
    reg_robot = reg.get_robot(agent["id"])
    if reg_robot:
        camera_port = reg_robot.camera_port

    mission = SearchMission(
        mission_id=str(uuid.uuid4())[:8],
        agent_id=agent["id"],
        host=agent["host"],
        camera_port=camera_port,
        target_type=target_type,
        target_name=target_name,
        task_goal=task_goal,
        search_pattern=search_pattern,
        report_all=report_all,
    )

    reg.set_busy(agent["id"], mission.mission_id)

    target_desc = f"'{target_name}'" if target_name else target_type
    mode_desc = "patrol" if search_pattern != "stationary" else "stationary"
    messages_log.append(AIMessage(
        content=f"Agent: {mode_desc} on {agent['id']}, watching for {target_desc}"
    ))
    logger.info(f"Starting agent mission: {mission.mission_id} ({mode_desc})")

    result = await run_search_mission(mission)

    agent_result = {
        "agent_id": agent["id"],
        "status": "success" if result["status"] in ("completed", "timeout") else "error",
        "response": (
            f"Agent {result['status']}: mode={mode_desc}, target={target_desc}, "
            f"frames={result['frames_analyzed']}, elapsed={result['elapsed_s']}s, "
            f"detections={len(result.get('detections', []))}"
        ),
        "structured": {"mission": result, "detections": result.get("detections", [])},
    }

    if result.get("match_result"):
        agent_result["response"] += f", match={result['match_result']}"

    messages_log.append(AIMessage(content=f"Agent: {result['status']}"))
    return {"agent_results": [agent_result], "messages": messages_log}


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
