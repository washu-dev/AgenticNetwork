from __future__ import annotations

from v3_main_agent.main_agent.models import AgentInvocationRecord, PIContext


class InMemoryStateStore:
    def __init__(self):
        self.contexts: dict[str, PIContext] = {}
        self.invocations: dict[str, AgentInvocationRecord] = {}
        self.pending_confirmations: dict[str, dict] = {}
        self.server_context_id: str | None = None

    def save_context(self, context: PIContext) -> PIContext:
        self.contexts[context.context_id] = context
        return context

    def save_server_context(self, context: PIContext) -> PIContext:
        self.server_context_id = context.context_id
        return self.save_context(context)

    def get_server_context(self) -> PIContext | None:
        if not self.server_context_id:
            return None
        return self.contexts.get(self.server_context_id)

    def save_invocation(self, invocation: AgentInvocationRecord) -> AgentInvocationRecord:
        self.invocations[invocation.task_id] = invocation
        return invocation

    def get_context(self, context_id: str) -> PIContext:
        return self.contexts[context_id]

    def get_invocation(self, task_id: str) -> AgentInvocationRecord:
        return self.invocations[task_id]

    def list_invocations_for_context(self, context_id: str) -> list[AgentInvocationRecord]:
        return [
            invocation
            for invocation in self.invocations.values()
            if invocation.context_id == context_id
        ]

    def save_pending_confirmation(self, context_id: str, pending: dict) -> dict:
        self.pending_confirmations[context_id] = pending
        return pending

    def get_pending_confirmation(self, context_id: str) -> dict | None:
        return self.pending_confirmations.get(context_id)

    def clear_pending_confirmation(self, context_id: str) -> None:
        self.pending_confirmations.pop(context_id, None)
