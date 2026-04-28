from __future__ import annotations

from v2_agent_network.services.common.models import AgentInvocationRecord, PIContext


class InMemoryStateStore:
    def __init__(self):
        self.contexts: dict[str, PIContext] = {}
        self.invocations: dict[str, AgentInvocationRecord] = {}

    def save_context(self, context: PIContext) -> PIContext:
        self.contexts[context.context_id] = context
        return context

    def save_invocation(self, invocation: AgentInvocationRecord) -> AgentInvocationRecord:
        self.invocations[invocation.task_id] = invocation
        return invocation

    def get_context(self, context_id: str) -> PIContext:
        return self.contexts[context_id]

    def get_invocation(self, task_id: str) -> AgentInvocationRecord:
        return self.invocations[task_id]

