import sys
from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Message, Part, Role, TextPart
from openai import AsyncOpenAI

from scout.agent import ScoutSession
from scout.capabilities.base import CapabilityRegistry
from scout.config import Settings


class ScoutAgentExecutor(AgentExecutor):
    """Wraps Scout as an A2A AgentExecutor.

    Each incoming request gets a fresh ScoutSession (no caller-state leakage between
    different remote callers). The shared CapabilityRegistry lets every call use the
    same Brave + peer tools that the local REPL uses.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        settings: Settings,
        openai_client: AsyncOpenAI,
        verbose: bool = False,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._openai = openai_client
        self._verbose = verbose

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = (context.get_user_input() or "").strip()
        if self._verbose:
            print(
                f"[scout.executor] incoming request "
                f"(task={context.task_id}, len={len(user_text)})",
                file=sys.stderr,
            )
        if not user_text:
            await self._reply(event_queue, "(empty request — provide a research mission)")
            return

        session = ScoutSession(
            registry=self._registry,
            settings=self._settings,
            client=self._openai,
            verbose=self._verbose,
        )
        try:
            reply = await session.turn(user_text)
        except Exception as e:
            reply = f"scout error: {type(e).__name__}: {e}"
            if self._verbose:
                print(f"[scout.executor] turn failed: {e}", file=sys.stderr)

        await self._reply(event_queue, reply)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Phase 2 v1: not wired to actually interrupt an in-flight ReAct loop.
        await self._reply(event_queue, "(cancelled)")

    async def _reply(self, event_queue: EventQueue, text: str) -> None:
        await event_queue.enqueue_event(
            Message(
                role=Role.agent,
                message_id=str(uuid4()),
                parts=[Part(root=TextPart(text=text))],
            )
        )
