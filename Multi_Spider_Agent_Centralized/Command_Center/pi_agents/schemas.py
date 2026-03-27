"""
Pydantic output schemas for Pi Agent Brain LLM calls.

Used with LangChain's .with_structured_output() to get reliable,
typed responses from the LLM instead of manual JSON parsing.
"""

from typing import List

from pydantic import BaseModel, Field


class ActionDecomposition(BaseModel):
    """LLM output: decompose a natural language command into robot actions."""
    actions: List[str] = Field(
        description="Ordered list of robot action names to execute"
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
