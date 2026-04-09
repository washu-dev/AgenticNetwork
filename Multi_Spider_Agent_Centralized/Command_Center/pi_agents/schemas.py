"""
Pydantic output schemas for Pi Agent Brain LLM calls.

Used with LangChain's .with_structured_output() to get reliable,
typed responses from the LLM instead of manual JSON parsing.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class ActionStep(BaseModel):
    """A single action with optional parameters."""
    action: str = Field(description="Action name from the available actions list")
    steps: int = Field(
        default=0,
        description="Number of steps/repetitions. 0 = use default. For turn_left_angle/turn_right_angle: number of turn steps.",
    )
    angle: int = Field(
        default=0,
        description="Angle parameter. 0 = use default. For turn_left_angle/turn_right_angle: leg swing amplitude (10-90).",
    )


class ActionDecomposition(BaseModel):
    """LLM output: decompose a natural language command into robot actions."""
    actions: List[ActionStep] = Field(
        description="Ordered list of robot actions to execute, each with optional parameters"
    )
    reason: str = Field(
        description="Brief explanation of why these actions were chosen and in this order"
    )


class ObservationDecision(BaseModel):
    """LLM output: decide what action to take based on camera observations."""
    action: str = Field(
        description="The single action to execute from the available actions list"
    )
    reason: str = Field(
        description="Brief explanation for why this action was chosen"
    )


class ObstacleDecision(BaseModel):
    """LLM output: decide how to handle an obstacle during patrol."""
    action: str = Field(
        description="Avoidance action: backward, turn_left, turn_right, or stop"
    )
    reason: str = Field(
        description="Brief explanation for the avoidance strategy"
    )


class TargetPlan(BaseModel):
    """LLM output: decide what to do when a target is first detected."""
    approach: bool = Field(
        description="True if the robot should move closer to the target before acting"
    )
    final_action: str = Field(
        description="The action to execute when close enough (e.g. 'wave', 'sit_down', 'MISSION_COMPLETE')"
    )
    reason: str = Field(
        description="Brief explanation of the plan"
    )


class PatrolStep(BaseModel):
    """A single step in a patrol route."""
    action: str = Field(description="Movement action: 'forward', 'turn_left', or 'turn_right'")
    value: int = Field(description="For forward: number of steps. For turns: degrees.")


class PatrolRoute(BaseModel):
    """LLM output: patrol route based on user's task goal."""
    pattern: str = Field(
        description="Route pattern: 'stationary' (stay in place), 'spin' (turn 360 in place), or 'area' (walk a route)"
    )
    steps: List[PatrolStep] = Field(
        description="Ordered list of movement steps."
    )
    reason: str = Field(description="Brief explanation of why this route was chosen")


class PatrolReport(BaseModel):
    """LLM output: structured summary after a patrol completes."""
    summary: str = Field(
        description="Natural language summary of what was observed during patrol"
    )
    targets_found: List[str] = Field(
        default_factory=list,
        description="List of UNIQUE target types detected (e.g. ['person', 'backpack'])"
    )
    target_count: int = Field(
        default=0,
        description="Estimated number of DISTINCT targets (deduplicated). "
                    "E.g. if the same person was seen 5 times across frames, count as 1."
    )
    anomalies: List[str] = Field(
        default_factory=list,
        description="Any unusual or noteworthy observations"
    )
    recommendation: str = Field(
        default="",
        description="Suggested follow-up action if any"
    )
