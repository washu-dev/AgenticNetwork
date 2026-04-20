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
    "Otherwise, output one or more phases.\n\n"
    "TASK TYPES (pick one per phase):\n"
    "  direct_control — movement/posture/gesture, no detection\n"
    "  agent — camera detection + robot actions\n\n"
    "TARGETING:\n"
    "  target_robot_ids: [] = all robots, ['id'] = specific robot\n"
    "  target_zone: 'A' = robots in zone A, '' = no zone filter\n"
    "  target_type: object, face, any, none\n"
    "  target_name: specific class or person (or '' if generic)\n"
    "  duration_s: seconds if user specifies ('for 5 seconds' → 5), else 0\n\n"
    "OUTPUT-CAPABILITY FLAGS — orthogonal, decide EACH INDEPENDENTLY\n"
    "These five booleans do NOT form a mode enum. Any combination is legal.\n"
    "Judge every flag on its own trigger, never by elimination of others.\n\n"
    "  report_all (bool): the user wants observation/report output instead of\n"
    "    act-on-first-detection. Triggers: 'report', 'describe', 'tell (me/us)',\n"
    "    'observe', 'what (you|I) (see|saw)', 'what's there', 'look around and …'.\n"
    "    Tense is irrelevant ('saw' counts the same as 'see').\n\n"
    "  vlm_describe_objects (bool): the user wants per-object appearance detail\n"
    "    (clothing, color, held items, distinguishing features of each thing).\n"
    "    Triggers: 'describe what (you|I) (see|saw)', 'describe it/them/the X',\n"
    "    'what does it look like', 'appearance', 'in detail', 'details',\n"
    "    'characterize'. If the verb is 'describe' with an object-ish complement,\n"
    "    set this true EVEN IF the sentence also has 'what you see/saw' — the\n"
    "    verb 'describe' wins.\n\n"
    "  vlm_describe_scene (bool): the user wants a scene/setting description —\n"
    "    environment as a whole (room type, outdoor/indoor, lighting, layout,\n"
    "    general context), NOT individual objects. Triggers: 'describe the\n"
    "    environment', 'describe the scene', 'describe the surroundings',\n"
    "    'describe where you are', 'describe the setting/place/area/room',\n"
    "    'what kind of place is this'. Independent of vlm_describe_objects.\n"
    "    'Describe what you see' → objects only. 'Describe the environment you\n"
    "    are in' → scene only. 'Describe everything in detail' → BOTH true.\n\n"
    "  vlm_verify (bool): the user names visual attributes YOLO can't verify:\n"
    "    colors ('red shirt'), clothing, held objects ('holding a weapon'),\n"
    "    behavior, or non-COCO entities ('tank', 'intruder'). Independent of others.\n\n"
    "  alarm_on_detection (bool): watch/alert tasks — robot stays passive, never\n"
    "    approaches, raises a banner on each hit. Triggers: 'watch', 'alert',\n"
    "    'alarm', 'notify', 'warn', 'guard'. Independent of others.\n\n"
    "HARD CONSTRAINT: if the prompt contains any of the report_all trigger words\n"
    "above, the plan MUST contain an `agent` phase with report_all=true.\n"
    "Never route reporting intent to a direct_control phase — direct_control has\n"
    "no perception and the request is silently dropped.\n\n"
    "GENERAL RULES:\n"
    "1. 'person'/'people' → target_type='object', target_name='person'.\n"
    "   'face' → target_type='face'.\n"
    "2. Robot brain decides movement pattern and response actions from the\n"
    "   description autonomously; you do NOT enumerate individual moves.\n\n"
    "EXAMPLES (each shows only the non-default fields per phase):\n"
    "  'Wave' → 1 phase:\n"
    "    direct_control, description='wave'\n"
    "  'robot-002 wave' → 1 phase:\n"
    "    direct_control, target_robot_ids=['crawler-alpha-patrol-002'], description='wave'\n"
    "  'Detect person, if found wave' → 1 phase:\n"
    "    agent, target_type='object', target_name='person', description='find person, wave on detection'\n"
    "  'Find person wearing red shirt' → 1 phase:\n"
    "    agent, target_type='object', target_name='person', vlm_verify=true,\n"
    "    description='find person wearing red shirt'\n"
    "  'Report what you see' → 1 phase:\n"
    "    agent, target_type='any', report_all=true, description='report what you see'\n"
    "  'Describe what you see' → 1 phase (objects, NOT scene):\n"
    "    agent, target_type='any', report_all=true, vlm_describe_objects=true,\n"
    "    description='describe what you see'\n"
    "  'Describe what you saw in front of you' → 1 phase (verb 'describe' wins):\n"
    "    agent, target_type='any', report_all=true, vlm_describe_objects=true,\n"
    "    description='describe what you saw in front of you'\n"
    "  'Describe the environment you are in' → 1 phase (scene, NOT objects):\n"
    "    agent, target_type='any', report_all=true, vlm_describe_scene=true,\n"
    "    description='describe the environment you are in'\n"
    "  'Describe your surroundings' → 1 phase (scene only):\n"
    "    agent, target_type='any', report_all=true, vlm_describe_scene=true,\n"
    "    description='describe your surroundings'\n"
    "  'Describe the room and what's in it' → 1 phase (BOTH scene and objects):\n"
    "    agent, target_type='any', report_all=true, vlm_describe_scene=true,\n"
    "    vlm_describe_objects=true, description='describe the room and what is in it'\n"
    "  'Detect person for 10 seconds' → 1 phase:\n"
    "    agent, target_type='object', target_name='person', duration_s=10,\n"
    "    description='detect person for 10 seconds'\n\n"
    "WATCH / ALARM:\n"
    "  'Watch for tank, alarm if detected' → 1 phase:\n"
    "    agent, target_type='object', target_name='person', report_all=true,\n"
    "    alarm_on_detection=true, vlm_verify=true  (tank is not COCO; person+VLM)\n"
    "  'Watch for intruders for 30 seconds' → 1 phase:\n"
    "    agent, target_type='object', target_name='person', report_all=true,\n"
    "    alarm_on_detection=true, duration_s=30\n"
    "  'Alert on person wearing red shirt for 5 minutes' → 1 phase:\n"
    "    agent, target_type='object', target_name='person', report_all=true,\n"
    "    alarm_on_detection=true, vlm_verify=true, duration_s=300\n"
    "  'Watch for anyone holding a weapon, alert me' → 1 phase:\n"
    "    agent, target_type='object', target_name='person', report_all=true,\n"
    "    alarm_on_detection=true, vlm_verify=true\n\n"
    "SEMANTIC-TO-COCO MAPPING (YOLO only knows 80 COCO classes):\n"
    "  'intruder'/'suspect'/'stranger'/'visitor'/'person in red' → target_name='person'\n"
    "  'vehicle'/'car'/'truck' → use exact COCO class ('car', 'truck'), else 'car'\n"
    "  'weapon'/'gun'/'knife' → target_name='person' + vlm_verify=true\n"
    "    (YOLO 'knife' is kitchen knives only; real weapons need VLM)\n"
    "  'animal' → target_type='any', target_name=''\n"
    "  Any non-COCO entity ('tank') → nearest COCO class + vlm_verify=true\n\n"
    "COMPOUND COMMANDS — SAME sequence for ALL targeted robots\n"
    "(multiple phases, same target_robot_ids across phases):\n"
    "  'Stand up and report what you see' → 2 phases:\n"
    "    [1] direct_control, description='stand up'\n"
    "    [2] agent, target_type='any', report_all=true, description='report what you see'\n"
    "  'Sit down then describe what you see' → 2 phases:\n"
    "    [1] direct_control, description='sit down'\n"
    "    [2] agent, target_type='any', report_all=true, vlm_describe_objects=true,\n"
    "        description='describe what you see'\n"
    "  'Stand up and describe what you saw in front of you' → 2 phases:\n"
    "    [1] direct_control, description='stand up'\n"
    "    [2] agent, target_type='any', report_all=true, vlm_describe_objects=true,\n"
    "        description='describe what you saw in front of you'\n"
    "  'Stand up and describe the environment' → 2 phases:\n"
    "    [1] direct_control, description='stand up'\n"
    "    [2] agent, target_type='any', report_all=true, vlm_describe_scene=true,\n"
    "        description='describe the environment'\n\n"
    "MOTION-AS-OBSERVATION (stays in ONE agent phase):\n"
    "When the motion IS the perception mechanism (spin, rotate, look around,\n"
    "walk, patrol, scan) combined with report/describe, output ONE `agent`\n"
    "phase whose description INCLUDES the motion verb. The robot brain plans\n"
    "the motion pattern and runs detection concurrently. Splitting into\n"
    "direct_control + agent would move blind then stand still observing.\n"
    "  'Spin and report what you saw' → 1 phase:\n"
    "    agent, target_type='any', report_all=true,\n"
    "    description='spin 360° and report what you see'\n"
    "  'Scan the area and report' → 1 phase:\n"
    "    agent, target_type='any', report_all=true,\n"
    "    description='scan the area and report what you see'\n"
    "  'Look around and describe everything in detail' → 1 phase (both flags):\n"
    "    agent, target_type='any', report_all=true, vlm_describe_scene=true,\n"
    "    vlm_describe_objects=true,\n"
    "    description='look around and describe everything in detail'\n"
    "  'Walk forward and report what you see' → 1 phase:\n"
    "    agent, target_type='any', report_all=true,\n"
    "    description='walk forward and report what you see'\n\n"
    "PER-ROBOT COMMANDS — different actions for different robots\n"
    "(one phase per robot, each phase's target_robot_ids has exactly that robot):\n"
    "  'robot 006 wave, robot 005 sit down' → 2 phases:\n"
    "    [1] direct_control, target_robot_ids=['crawler-alpha-patrol-006'], description='wave'\n"
    "    [2] direct_control, target_robot_ids=['crawler-alpha-patrol-005'], description='sit down'\n"
    "  'robot 006 stand up and wave, robot 005 push up, robot 001 stand up' → 3 phases\n"
    "    (robot 006's compound stays as ONE phase; Pi brain handles the sub-sequence):\n"
    "    [1] direct_control, target_robot_ids=['crawler-alpha-patrol-006'],\n"
    "        description='stand up and wave'\n"
    "    [2] direct_control, target_robot_ids=['crawler-alpha-patrol-005'], description='push up'\n"
    "    [3] direct_control, target_robot_ids=['crawler-alpha-patrol-001'], description='stand up'\n\n"
    "PHASE-SPLITTING RULES:\n"
    "- Each phase's description contains ONLY that phase's action (never the whole compound).\n"
    "- Per-robot commands: exactly one phase per robot, target_robot_ids has that robot.\n"
    "- 'All robots X and Y': same (empty) target list across phases.\n"
    "- Match short names ('robot 006', '006', 'zone A') to the full robot IDs below."
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

async def parse_intent(prompt: str, online_robots: list[dict]) -> TaskIntent | None:
    """Parse user prompt into TaskIntent via a single structured-output LLM call.

    The TaskIntent schema embeds all output-capability flags (report_all,
    vlm_describe_objects, vlm_describe_scene, vlm_verify, alarm_on_detection)
    as orthogonal booleans on each phase, so one call fills the entire plan
    and no secondary tool call can disagree with it.
    """
    parse_prompt = build_parse_prompt(online_robots)
    try:
        llm = create_llm()
        intent_llm = llm.with_structured_output(TaskIntent, strict=True)
        intent = await intent_llm.ainvoke([
            SystemMessage(content=parse_prompt),
            HumanMessage(content=prompt),
        ])
        return intent
    except Exception as e:
        logger.warning(f"LLM parse failed: {e}, using fallback")
        return None


# ---------------------------------------------------------------------------
# Fallback keyword-based classification (no LLM)
# ---------------------------------------------------------------------------

def fallback_intent(prompt: str) -> dict:
    """Keyword-based fallback when the LLM parse fails.

    Sets each output-capability flag from its own independent keyword set,
    mirroring the orthogonality the LLM prompt enforces.
    """
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

    # Independent flag checks — each flag gets its own keyword set.
    report_all = any(kw in p for kw in [
        "report", "describe", "tell me", "tell us",
        "what do you see", "what you see", "what you saw",
        "what's there", "whats there", "look around", "observe",
    ])
    # Scene keywords win over object keywords when both could apply, because
    # scene-level language (environment / surroundings / setting) is more specific.
    _SCENE_KW = [
        "environment", "surroundings", "setting", "scene",
        "where you are", "where we are", "the place", "the area",
        "the room", "the space", "what kind of place",
    ]
    vlm_describe_scene = "describe" in p and any(kw in p for kw in _SCENE_KW)
    # Object-level: "describe" with object-ish complements or generic "in detail".
    _OBJECT_KW = [
        "what you see", "what you saw", "what do you see",
        "it", "them", "the person", "the object",
        "appearance", "what does it look like",
    ]
    vlm_describe_objects = (
        ("describe" in p and any(kw in p for kw in _OBJECT_KW) and not vlm_describe_scene)
        or any(kw in p for kw in ["appearance", "in detail", "details", "characterize"])
    )
    vlm_verify = any(kw in p for kw in [
        "wearing", "colored", "color ", "holding", "carrying",
        "red ", "blue ", "green ", "yellow ", "black ", "white ",
    ])
    alarm_on_detection = any(kw in p for kw in [
        "watch", "alert", "alarm", "notify", "warn", "guard",
    ])

    is_direct = any(kw in p for kw in _DIRECT_KEYWORDS)
    is_detection = any(kw in p for kw in ["detect", "search", "find", "patrol", "scan", "look for"])

    # Pure movement with no perception intent → direct_control.
    if is_direct and not is_detection and not report_all and not alarm_on_detection:
        return {
            "phases": [{
                "target_robot_ids": [], "task_type": "direct_control",
                "target_type": "none", "description": prompt,
            }],
            "urgency": "medium",
        }

    if any(kw in p for kw in ["face", "faces"]):
        target = "face"
    elif any(kw in p for kw in ["person", "people", "human", "intruder", "suspect", "stranger"]):
        target = "object"
    elif any(kw in p for kw in ["qr", "barcode", "code"]):
        target = "qr_code"
    else:
        target = "any"

    return {
        "phases": [{
            "target_robot_ids": [], "task_type": "agent", "target_type": target,
            "description": prompt,
            "report_all": report_all,
            "vlm_describe_objects": vlm_describe_objects,
            "vlm_describe_scene": vlm_describe_scene,
            "vlm_verify": vlm_verify,
            "alarm_on_detection": alarm_on_detection,
        }],
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
