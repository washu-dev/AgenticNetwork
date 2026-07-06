"""generator — turn a user's answers about an external endpoint into a complete,
discoverable LLM-wrapper agent that lands in the agent-directory repo.

Pure assembly (answers -> files); no runtime deps beyond stdlib. The pipeline:

    AgentAnswers ──> card_builder  ──> <agent>.card.json   (résumé, routable)
                 ──> prompt_builder ──> system prompt        (understands the I/O)
                 ──> repo_emitter   ──> a full agent folder in agent-directory/
                                        (card + server + vendored engine + Docker)

The emitted agent self-publishes to ADS on startup via the shared
ads_utils.publisher and runs the endpoint_wrapper_spec engine — i.e. it is the
"user-hosted endpoint, LLM wrapper that understands the I/O" path made concrete.
"""
from __future__ import annotations

from .models import AgentAnswers, SkillAnswer

__all__ = ["AgentAnswers", "SkillAnswer"]
