"""The answers a user gives about their endpoint — the generator's only input.

These map directly onto the registration UI questions: identity, where the
endpoint is + how to authenticate, what the agent can do (skills), and what the
endpoint's input/output look like (so the wrapper can "understand the I/O").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


def slugify(text: str) -> str:
    """'Weather Bridge Agent' -> 'weather-bridge-agent' (used for dir + skill ids)."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "agent"


@dataclass
class SkillAnswer:
    """One capability the wrapped agent advertises."""

    name: str
    description: str
    examples: list[str] = field(default_factory=list)
    # Path-style key into the OASF taxonomy (registration_service/oasf_skills.py),
    # used to make the agent DISCOVERABLE. At least one skill across the agent must
    # set this, or the card cannot be routed (planner never sees it).
    oasf_key: str | None = None
    # Marks a side-effecting capability (gates write-confirmation policy downstream).
    write: bool = False


@dataclass
class AgentAnswers:
    """Everything needed to generate a wrapper agent for a user-hosted endpoint."""

    # --- Identity -----------------------------------------------------------
    name: str
    description: str

    # --- The wrapped endpoint ----------------------------------------------
    endpoint_url: str
    protocol: str = "http"             # 'http' (plain JSON) | 'a2a' (JSON-RPC)
    # NAME of the env var holding the upstream token (never the token itself).
    auth_env: str | None = None

    # --- Capabilities -------------------------------------------------------
    skills: list[SkillAnswer] = field(default_factory=list)

    # --- I/O understanding (woven into the system prompt) -------------------
    input_description: str = ""
    output_description: str = ""
    example_request: str = ""
    example_response: str = ""

    # --- Deployment ---------------------------------------------------------
    port: int = 19110
    model: str = "openai/gpt-oss-120b"  # must equal the vLLM --served-model-name
    version: str = "0.1.0"
    provider_org: str = "AgenticNetwork (generated)"

    @property
    def slug(self) -> str:
        return slugify(self.name)

    def validate(self) -> None:
        problems: list[str] = []
        if not self.name.strip():
            problems.append("name is required")
        if not self.description.strip():
            problems.append("description is required")
        if not self.endpoint_url.strip():
            problems.append("endpoint_url is required")
        if self.protocol not in ("http", "a2a"):
            problems.append(f"protocol must be 'http' or 'a2a', got {self.protocol!r}")
        if not self.skills:
            problems.append("at least one skill is required")
        if not any(s.oasf_key for s in self.skills):
            problems.append(
                "at least one skill must set oasf_key (an OASF taxonomy path), "
                "otherwise the agent cannot be routed/discovered"
            )
        if problems:
            raise ValueError("Invalid AgentAnswers:\n  - " + "\n  - ".join(problems))
