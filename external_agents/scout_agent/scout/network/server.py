import sys

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard
from agntcy_app_sdk.factory import AgntcyFactory

from scout.network.executor import ScoutAgentExecutor


async def start_a2a_server(
    *,
    card: AgentCard,
    executor: ScoutAgentExecutor,
    factory_name: str = "scout-factory",
    max_sessions: int = 10,
    keep_alive: bool = False,
) -> AgntcyFactory:
    """Bring up the A2A server. The transport is determined by the card's
    `preferred_transport` and `additional_interfaces` — JSONRPC opens a
    Starlette HTTP server on the URL's host:port.
    """
    handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore()
    )
    factory = AgntcyFactory(name=factory_name)
    session = factory.create_app_session(max_sessions=max_sessions)
    print(
        f"[scout.server] bringing up A2A server (transport={card.preferred_transport}, "
        f"interfaces={[i.url for i in (card.additional_interfaces or [])]})",
        file=sys.stderr,
    )
    builder = session.add_a2a_card(card, handler)
    await builder.start(keep_alive=keep_alive)
    return factory


async def stop_a2a_server(factory: AgntcyFactory) -> None:
    try:
        if hasattr(factory, "close"):
            await factory.close()  # type: ignore[attr-defined]
    except Exception as e:
        print(f"[scout.server] factory shutdown warning: {e}", file=sys.stderr)
