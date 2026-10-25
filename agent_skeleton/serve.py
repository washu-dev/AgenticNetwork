"""ZONE 6 — Serve.  ENGINE

Wires the executor into an HTTP server. create_app builds the A2A web app for an
AgentSpec (there is no default agent); container_runner uses it to serve built
images of ``llm`` agents with uploaded tools, generated wrapper agents use it too,
and so does the demo agent (``python -m agent_skeleton.demo serve``). The CLI below
wraps one endpoint (serve-wrapper).

Directory (ADS) publishing is not done here: the registration service's
supervisor publishes registered agents, and a hand-run agent can be published with
``python -m agentnet.scripts.publish_agents --card <card.json>``.
The runtime layering:

    uvicorn (HTTP server)
      -> A2AStarletteApplication(card, handler).build()   (Starlette/ASGI app)
        -> DefaultRequestHandler(executor, InMemoryTaskStore())  (A2A protocol)
          -> SkeletonAgentExecutor.execute(context, event_queue)  (runs the spec)

Run (the headline path — wrap an external endpoint in an LLM loop):
    python -m agent_skeleton.serve serve-wrapper --card my.card.json \
        --endpoint-url https://api.example.com/run
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .a2a_runtime import (
    A2AStarletteApplication,
    AgentCard,
    DefaultRequestHandler,
    InMemoryTaskStore,
    require_a2a,
)
from .config import (
    AGENT_NAME,
    env_advertise_url,
    env_endpoint_auth_env,
    env_endpoint_method,
    env_endpoint_protocol,
    env_endpoint_url,
    env_host,
    env_model,
    env_port,
)
from .executor import SkeletonAgentExecutor

if TYPE_CHECKING:
    from .spec import AgentSpec


def load_agent_card(card_path: Path | str) -> Any:
    require_a2a()
    with Path(card_path).open("r", encoding="utf-8") as fh:
        return AgentCard.model_validate(json.load(fh))


def create_app(agent_card: Any, model: str | None = None, *, spec: "AgentSpec") -> Any:
    require_a2a()
    # Fail fast if the spec's schemas and functions disagree (the check disaster lacks).
    spec.validate()
    handler = DefaultRequestHandler(
        agent_executor=SkeletonAgentExecutor(model=model, spec=spec),
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(agent_card=agent_card, http_handler=handler).build()


# --- CLI ------------------------------------------------------------------

def _add_serve_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", default=env_model())
    p.add_argument("--host", default=env_host())
    p.add_argument("--port", type=int, default=env_port())
    p.add_argument("--advertise-url", default=env_advertise_url())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve an A2A agent that wraps an external endpoint")
    sub = parser.add_subparsers(dest="command", required=True)

    # The end-goal entry point: serve an LLM loop that wraps an external endpoint.
    wrap = sub.add_parser(
        "serve-wrapper",
        help="Run the A2A server as an LLM wrapper over an external endpoint.",
    )
    _add_serve_args(wrap)
    wrap.add_argument("--card", type=Path, required=True, help="This agent's card (JSON).")
    wrap.add_argument("--endpoint-url", default=env_endpoint_url(), help="URL of the external service to wrap.")
    wrap.add_argument("--endpoint-protocol", default=env_endpoint_protocol(), choices=["http", "a2a"])
    wrap.add_argument("--endpoint-method", default=env_endpoint_method())
    wrap.add_argument(
        "--endpoint-auth-env",
        default=env_endpoint_auth_env(),
        help="NAME of the env var holding the upstream auth token (not the token).",
    )
    wrap.add_argument("--io-criteria", default="", help="Notes on the endpoint's expected input/output.")
    return parser


def _serve(args: argparse.Namespace, spec: "AgentSpec") -> None:
    require_a2a()
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("The uvicorn package is required to serve.") from exc

    card = load_agent_card(args.card)
    if args.advertise_url:
        try:
            card = card.model_copy(update={"url": args.advertise_url})
        except Exception:
            card.url = args.advertise_url

    print(f"Serving {getattr(card, 'name', AGENT_NAME)} on {args.host}:{args.port} (url={getattr(card, 'url', '')})")
    uvicorn.run(create_app(card, model=args.model, spec=spec), host=args.host, port=args.port)


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "serve-wrapper":
        if not args.endpoint_url:
            raise SystemExit("serve-wrapper requires --endpoint-url (or AGENT_ENDPOINT_URL).")
        from .spec import endpoint_wrapper_spec
        from .system_tools.call_endpoint import EndpointConfig

        card = load_agent_card(args.card)
        endpoint = EndpointConfig(
            url=args.endpoint_url,
            method=args.endpoint_method,
            protocol=args.endpoint_protocol,
            auth_env=args.endpoint_auth_env,
        )
        spec = endpoint_wrapper_spec(
            name=getattr(card, "name", AGENT_NAME),
            endpoint=endpoint,
            description=getattr(card, "description", "") or "",
            io_criteria=args.io_criteria,
            model=args.model,
        )
        _serve(args, spec=spec)
        return


if __name__ == "__main__":
    main()
