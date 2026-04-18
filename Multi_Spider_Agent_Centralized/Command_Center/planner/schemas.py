"""
Pydantic output schemas for Planner Agent LLM calls.

Used with LangChain's .with_structured_output() for task parsing.
"""

from typing import List, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


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
    description: str = Field(description="One-line description")
    target_name: str = Field(default="", description="For face search: specific person name")
    report_all: bool = Field(default=False, description="True if observe/report task")
    duration_s: int = Field(
        default=0,
        description="Duration in seconds if user specifies (e.g. 'for 5 seconds' → 5). 0 = use default timeout.",
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
