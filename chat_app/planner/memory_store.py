"""The planner's in-process memory: working conversation contexts and a cache of
pending confirmations. It lives in this process only and resets on restart; with a
database configured, chat_app/store/ keeps the durable copies."""

from __future__ import annotations

from chat_app.planner.models import ConversationContext


class MemoryStore:
    def __init__(self):
        self.contexts: dict[str, ConversationContext] = {}
        self.pending_confirmations: dict[str, dict] = {}

    def save_context(self, context: ConversationContext) -> ConversationContext:
        self.contexts[context.context_id] = context
        return context

    def get_context(self, context_id: str) -> ConversationContext:
        return self.contexts[context_id]

    def save_pending_confirmation(self, context_id: str, pending: dict) -> dict:
        self.pending_confirmations[context_id] = pending
        return pending

    def get_pending_confirmation(self, context_id: str) -> dict | None:
        return self.pending_confirmations.get(context_id)

    def clear_pending_confirmation(self, context_id: str) -> None:
        self.pending_confirmations.pop(context_id, None)

    def take_pending_confirmation(self, context_id: str, expected: dict) -> dict | None:
        """Remove and return the pending confirmation if it is still ``expected``, else None."""
        if self.pending_confirmations.get(context_id) != expected:
            return None
        return self.pending_confirmations.pop(context_id)
