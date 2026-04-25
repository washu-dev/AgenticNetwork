"""
Pydantic output schemas for Planner Agent LLM calls.

Used with LangChain's .with_structured_output() for task parsing.
"""

from typing import List, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class Phase(BaseModel):
    """A single execution phase describing what robots should do.

    Output flags (report_all, vlm_describe_objects, vlm_describe_scene,
    vlm_verify, alarm_on_detection) are ORTHOGONAL — each decides one
    capability on its own. Never pick between them as if they were a mode
    enum: "describe what you saw" sets BOTH report_all AND
    vlm_describe_objects. "Describe the environment you're in" sets BOTH
    report_all AND vlm_describe_scene.
    """
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
    description: str = Field(description="One-line description")
    target_name: str = Field(default="", description="For face search: specific person name")
    duration_s: int = Field(
        default=0,
        description="Duration in seconds if user specifies (e.g. 'for 5 seconds' → 5). 0 = use default timeout.",
    )

    # ── Orthogonal output-capability flags ──
    # Each of the four flags below is an INDEPENDENT yes/no. Any combination
    # is legal. Do NOT treat them as a single mode — they were split up
    # precisely to avoid the "report vs describe" conflation that caused
    # keyword rules to conflict.

    report_all: bool = Field(
        default=False,
        description=(
            "True whenever the user asks for observation/report output — robot "
            "stays passive, collects YOLO labels over the whole duration rather "
            "than stopping at first detection. Trigger words: 'report', 'describe', "
            "'tell (me/us)', 'observe', 'what (you/I) (see|saw)', 'what's there', "
            "'look around and ...'. Past tense ('saw') counts the same as present."
        ),
    )
    vlm_describe_objects: bool = Field(
        default=False,
        description=(
            "True when the user wants per-object appearance detail — clothing, "
            "color, held items, size, distinguishing features of each detected "
            "thing. Triggers: 'describe what you see/saw', 'describe it/them', "
            "'what does it look like', 'appearance', 'in detail', 'details', "
            "'characterize'. The verb 'describe' with an object-ish complement "
            "('what you see', 'it', 'them', 'the person') wins even if the "
            "sentence also contains 'what you saw'. "
            "'Report what you see' → false. 'Describe what you see' → true. "
            "'Describe the person' → true."
        ),
    )
    vlm_describe_scene: bool = Field(
        default=False,
        description=(
            "True when the user wants a scene/setting description — the "
            "environment as a whole (room type, outdoor/indoor, lighting, "
            "layout, general context), not individual objects. Triggers: "
            "'describe the environment', 'describe the scene', 'describe the "
            "surroundings', 'describe where you are', 'describe the setting', "
            "'describe the place/area/room', 'what kind of place/room is this'. "
            "Decide this INDEPENDENTLY of vlm_describe_objects — the user may "
            "want both (e.g. 'describe everything in detail' → both true) or "
            "just one. 'Describe what you see' → objects, not scene. "
            "'Describe the environment you are in' → scene, not objects. "
            "'Describe the environment and what's in it' → both true."
        ),
    )
    vlm_verify: bool = Field(
        default=False,
        description=(
            "True when the user names specific visual attributes on a target that "
            "YOLO's label alone cannot verify: colors ('red shirt'), clothing, "
            "held objects ('holding a weapon'), behavior, or named entities that "
            "aren't COCO classes ('tank', 'suspect'). Decide independently of "
            "report_all, vlm_describe_objects, and vlm_describe_scene."
        ),
    )
    alarm_on_detection: bool = Field(
        default=False,
        description=(
            "True for watch/alarm tasks: robot stays in observe mode for the whole "
            "duration and does NOT stop on first detection. Each new target match "
            "emits an Alarm: event surfaced as a flashing red banner. Trigger "
            "words: 'watch', 'alert', 'alarm', 'notify', 'warn', 'guard'. "
            "Decide independently of the other flags."
        ),
    )
    route_prompt: Optional[str] = Field(
        default=None,
        description=(
            "When the user dictates a SPECIFIC walking route in the prompt, place "
            "ONLY the route portion here as a natural-language string the Robot "
            "Agent's LLM will turn into action steps.  Examples:\n"
            "  'Patrol forward 10 steps then turn left 90 degrees and report'\n"
            "    → route_prompt='forward 10 steps, turn left 90 degrees'\n"
            "  'Walk forward 5 steps, scan, then forward 5 more, look for tank'\n"
            "    → route_prompt='forward 5 steps, then forward 5 more steps'\n"
            "Triggers: any prompt containing concrete locomotion steps "
            "('forward N steps', 'backward', 'turn left/right N degrees', "
            "'walk to', 'go around the corner').  When set, the patrol "
            "pattern becomes 'custom' (overrides stationary/spin defaults) "
            "and the robot walks the route while running detection in "
            "parallel.  Always pair with task_type='agent' and report_all=true "
            "(or any of the describe/alarm flags) so observations are still "
            "collected during the walk.  Leave None when the user did not "
            "specify a route."
        ),
    )


class TaskIntent(BaseModel):
    is_valid_task: bool = Field(description="False if prompt is unrelated to robot control")
    rejection_reason: str = Field(default="", description="If is_valid_task=false, reason why")
    phases: List[Phase] = Field(description=(
        "One or more phases executed sequentially. Use multiple phases ONLY for "
        "compound commands like 'stand up and report', 'sit down then describe'. "
        "Single-action commands should be a single phase."
    ))
    urgency: str = Field(description="low / medium / high")


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
