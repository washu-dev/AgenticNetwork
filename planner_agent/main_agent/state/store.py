from __future__ import annotations

from planner_agent.main_agent.models import PIContext


class InMemoryStateStore:
    def __init__(self):
        self.contexts: dict[str, PIContext] = {}
        self.pending_confirmations: dict[str, dict] = {}

    def save_context(self, context: PIContext) -> PIContext:
        self.contexts[context.context_id] = context
        return context

    def get_context(self, context_id: str) -> PIContext:
        return self.contexts[context_id]

    def save_pending_confirmation(self, context_id: str, pending: dict) -> dict:
        self.pending_confirmations[context_id] = pending
        return pending

    def get_pending_confirmation(self, context_id: str) -> dict | None:
        return self.pending_confirmations.get(context_id)

    def clear_pending_confirmation(self, context_id: str) -> None:
        self.pending_confirmations.pop(context_id, None)
