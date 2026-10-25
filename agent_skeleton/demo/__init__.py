"""The demo agent: a small, complete ``llm`` agent with two text tools.  DEMO AGENT

SAFE TO EDIT. Nothing in the runtime imports this package, and it is left out of
everything the platform ships: the installed package (pyproject.toml), the copy
vendored into built images (registration_service/supervisor.py) and the generator's
``wrapper_engine/`` (generator/repo_emitter.py). A change here changes only the
demo. Everything else in agent_skeleton/ is runtime that reaches every hosted agent.

  * ``__init__.py``      SYSTEM_PROMPT + demo_spec(), the demo's AgentSpec
  * ``tools.py``         TOOL_SCHEMAS + tool bodies + TOOL_REGISTRY, shaped like an
                         uploaded tools module
  * ``agent.card.json``  the demo's card ("Skeleton Agent")
  * ``__main__.py``      ``python -m agent_skeleton.demo check|serve``
"""
from __future__ import annotations

from pathlib import Path

from ..config import AGENT_NAME
from ..prompt import normalize_result
from ..spec import AgentSpec
from .tools import TOOL_REGISTRY, TOOL_SCHEMAS

CARD_PATH = Path(__file__).resolve().with_name("agent.card.json")

# The demo agent's instructions and the exact output contract it asks of the model
# (the keys normalize_result reads).
SYSTEM_PROMPT = (
    "You are a helpful assistant agent. Use the available tools when they help "
    "answer the user's request, then produce a final answer. "
    "Return ONLY valid JSON with these keys: "
    'answer (string), tools_used (array of strings). '
    "Do not include any prose outside the JSON object."
)


def demo_spec() -> AgentSpec:
    """Build the demo agent's spec: SYSTEM_PROMPT plus the tools in tools.py."""
    return AgentSpec(
        name=AGENT_NAME,
        system_prompt=SYSTEM_PROMPT,
        tool_schemas=list(TOOL_SCHEMAS),
        tool_registry=dict(TOOL_REGISTRY),
        normalize=normalize_result,
        mode="llm",
    )
