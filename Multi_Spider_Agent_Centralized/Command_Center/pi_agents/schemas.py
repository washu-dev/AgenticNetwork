"""
Pydantic output schemas for Pi Agent Brain LLM calls.

Used with LangChain's .with_structured_output() to get reliable,
typed responses from the LLM instead of manual JSON parsing.
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ActionStep(BaseModel):
    """A single action with optional parameters."""
    action: str = Field(description="Action name from the available actions list")
    params: Optional[Dict] = Field(
        default=None,
        description="Optional parameters, e.g. {\"steps\": 3, \"angle\": 20} for turn_left_angle",
    )


class ActionDecomposition(BaseModel):
    """LLM output: decompose a natural language command into robot actions."""
    steps: List[ActionStep] = Field(
        description="Ordered list of robot actions to execute, each with optional parameters"
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


class PatrolRoute(BaseModel):
    """LLM output: patrol route based on user's task goal."""
    pattern: str = Field(
        description="Route pattern: 'stationary' (stay in place), 'spin' (turn 360 in place), or 'area' (walk a route)"
    )
    steps: List[dict] = Field(
        description="Ordered list of movement steps. Each step: {action: 'forward'|'turn_left'|'turn_right', value: int}. "
                    "For forward: value = number of steps. For turns: value = degrees."
    )
    reason: str = Field(description="Brief explanation of why this route was chosen")


class PatrolReport(BaseModel):
    """LLM output: structured summary after a patrol completes."""
    summary: str = Field(
        description="Natural language summary of what was observed during patrol"
    )
    targets_found: List[str] = Field(
        default_factory=list,
        description="List of target types detected (e.g. ['person', 'backpack'])"
    )
    anomalies: List[str] = Field(
        default_factory=list,
        description="Any unusual or noteworthy observations"
    )
    recommendation: str = Field(
        default="",
        description="Suggested follow-up action if any"
    )
