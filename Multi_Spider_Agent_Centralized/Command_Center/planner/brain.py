"""
Planner Agent LLM calls: task parsing and fallback intent classification.

Corresponds to pi_agents/brain.py for the Robot Agent.
"""

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import create_llm
from .schemas import TaskIntent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompt for task parsing
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
    "  report_all: true = just observe/report, false = act on detection\n"
    "  duration_s: seconds if user specifies (e.g. 'for 5 seconds' → 5). 0 = default.\n\n"
    "RULES:\n"
    "1. 'person'/'people' → target_type='object'. 'face' → target_type='face'.\n"
    "2. Robot decides movement pattern and response actions from task_goal autonomously.\n\n"
    "EXAMPLES:\n"
    "  'Wave' → ONE phase: direct_control (description='wave')\n"
    "  'robot-002 wave' → ONE phase: direct_control, target_robot_ids=['robot-002']\n"
    "  'Detect person, if found wave' → ONE phase: agent, target_type='object'\n"
    "  'Find person wearing red shirt' → ONE phase: agent, target_type='object' + call enable_vlm(mode='verify')\n"
    "  'Report what you see' → ONE phase: agent, target_type='any', report_all=true\n"
    "  'Describe what you see' → ONE phase: agent, target_type='any', report_all=true + call enable_vlm(mode='describe')\n"
    "  'Detect person for 10 seconds' → ONE phase: agent, target_type='object', duration_s=10\n\n"
    "COMPOUND COMMANDS — SAME action sequence for ALL targeted robots\n"
    "(output MULTIPLE phases in order, each phase with the SAME target_robot_ids):\n"
    "  'Stand up and report what you see'\n"
    "    → TWO phases:\n"
    "       [1] direct_control, target_robot_ids=[], description='stand up'\n"
    "       [2] agent, target_robot_ids=[], target_type='any', report_all=true,\n"
    "           description='report what you see'\n"
    "  'Sit down then describe what you see'\n"
    "    → TWO phases:\n"
    "       [1] direct_control, description='sit down'\n"
    "       [2] agent, target_type='any', report_all=true, description='describe what you see'\n"
    "         + call enable_vlm(mode='describe')\n\n"
    "PER-ROBOT COMMANDS — different actions for different robots\n"
    "(output ONE phase PER ROBOT, each with its own target_robot_ids):\n"
    "  'robot 006 wave, robot 005 sit down'\n"
    "    → TWO phases:\n"
    "       [1] direct_control, target_robot_ids=['crawler-alpha-patrol-006'], description='wave'\n"
    "       [2] direct_control, target_robot_ids=['crawler-alpha-patrol-005'], description='sit down'\n"
    "  'robot 006 stand up and wave, robot 005 push up, robot 001 stand up'\n"
    "    → THREE phases (note: robot 006's compound is a SINGLE phase — Pi brain handles\n"
    "       the 'stand up and wave' sub-sequence on that one robot):\n"
    "       [1] direct_control, target_robot_ids=['crawler-alpha-patrol-006'],\n"
    "           description='stand up and wave'\n"
    "       [2] direct_control, target_robot_ids=['crawler-alpha-patrol-002'],\n"
    "           description='push up'\n"
    "       [3] direct_control, target_robot_ids=['crawler-alpha-patrol-001'],\n"
    "           description='stand up'\n"
    "\nRULES for splitting into phases:\n"
    "- Each phase's description MUST contain ONLY that phase's action (never the whole compound).\n"
    "- Per-robot commands: ONE phase per robot. Each phase's target_robot_ids has exactly that robot.\n"
    "- 'All robots X and Y': SAME target for both phases (empty list = all online).\n"
    "- Match short names like 'robot 006', '006', 'zone A' to the full robot ID from AVAILABLE ROBOTS."
)


# ---------------------------------------------------------------------------
# Build prompt with robot context
# ---------------------------------------------------------------------------

def build_parse_prompt(online_robots: list[dict]) -> str:
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
# LLM parse call
# ---------------------------------------------------------------------------

async def parse_intent(prompt: str, online_robots: list[dict]) -> tuple[TaskIntent | None, dict]:
    """Parse user prompt into TaskIntent via LLM.

    Uses two-step process:
    1. LLM decides whether to call VLM tools (tool calling)
    2. LLM outputs structured TaskIntent

    Returns: (TaskIntent or None, vlm_config: dict)
        vlm_config: {"verify": bool, "describe": bool}
    """
    parse_prompt = build_parse_prompt(online_robots)
    vlm_config = {"verify": False, "describe": False}

    try:
        llm = create_llm()

        # Step 1: Let LLM decide which VLM tools to enable
        from .tools import OPENAI_VLM_TOOLS
        llm_with_tools = llm.bind_tools(OPENAI_VLM_TOOLS)
        tool_response = await llm_with_tools.ainvoke([
            SystemMessage(content=parse_prompt),
            HumanMessage(content=prompt),
        ])

        for tool_call in tool_response.tool_calls:
            if tool_call["name"] == "enable_vlm":
                mode = tool_call["args"].get("mode", "verify")
                reason = tool_call["args"].get("reason", "")
                vlm_config[mode] = True
                logger.info(f"LLM called enable_vlm(mode={mode}): {reason}")

        # Step 2: Parse intent with structured output
        intent_llm = llm.with_structured_output(TaskIntent, strict=True)
        intent = await intent_llm.ainvoke([
            SystemMessage(content=parse_prompt),
            HumanMessage(content=prompt),
        ])

        return intent, vlm_config

    except Exception as e:
        logger.warning(f"LLM parse failed: {e}, using fallback")
        return None, vlm_config


# ---------------------------------------------------------------------------
# Fallback keyword-based classification (no LLM)
# ---------------------------------------------------------------------------

def fallback_intent(prompt: str) -> dict:
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
                        "description": prompt}],
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

    is_report = any(kw in p for kw in ["report", "observe", "what do you see", "look around"])

    return {
        "phases": [{"target_robot_ids": [], "task_type": "agent", "target_type": target,
                    "description": prompt, "report_all": is_report}],
        "urgency": "medium",
    }


# ---------------------------------------------------------------------------
# ReAct coordination prompt
# ---------------------------------------------------------------------------

def build_coordinator_prompt(robots: list[dict]) -> str:
    """Build system prompt for multi-robot ReAct coordinator."""
    robot_desc = ", ".join(f"{r['id']}(zone {r.get('zone', '?')})" for r in robots)
    return (
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
