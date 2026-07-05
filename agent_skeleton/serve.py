"""ZONE 6 — Serve + ADS publishing.  (COPY; rarely edit)

Wires the executor into an HTTP server and (optionally) registers the agent in
the directory. The runtime layering:

    uvicorn (HTTP server)
      -> A2AStarletteApplication(card, handler).build()   (Starlette/ASGI app)
        -> DefaultRequestHandler(executor, InMemoryTaskStore())  (A2A protocol)
          -> SkeletonAgentExecutor.execute(context, event_queue)  (your code)

Mirrors disaster_response_agent.py:925-953, 1018-1138, 2391-2452.

Improvement over disaster: publish_card_to_ads() polls `dirctl routing list`
after publishing to confirm the record actually ROUTED. Disaster skips this,
which is how an agent can land "stored but not routed" = invisible to the
planner (see the parent repo CLAUDE.md §2).

Run:
    python -m agent_skeleton.serve check          # validate schemas <-> functions
    python -m agent_skeleton.serve serve-a2a      # demo tools (no publish)
    python -m agent_skeleton.serve serve-a2a --publish-ads
    # the headline path — wrap an external endpoint in an LLM loop:
    python -m agent_skeleton.serve serve-wrapper --card my.card.json \
        --endpoint-url https://api.example.com/run
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .a2a_runtime import (
    A2AStarletteApplication,
    AgentCard,
    DefaultRequestHandler,
    InMemoryTaskStore,
    require_a2a,
)
from .config import (
    AGENT_NAME,
    DEFAULT_CARD_PATH,
    env_ads_addr,
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
from .tools import validate_tool_registry


def load_agent_card(card_path: Path | str = DEFAULT_CARD_PATH) -> Any:
    require_a2a()
    with Path(card_path).open("r", encoding="utf-8") as fh:
        return AgentCard.model_validate(json.load(fh))


def create_app(agent_card: Any, model: str | None = None, spec: Any = None) -> Any:
    require_a2a()
    # Fail fast if the schemas and functions disagree (the check disaster lacks).
    # With a spec, validate ITS tools; without one, validate the demo defaults.
    if spec is not None:
        spec.validate()
    else:
        validate_tool_registry()
    handler = DefaultRequestHandler(
        agent_executor=SkeletonAgentExecutor(model=model, spec=spec),
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(agent_card=agent_card, http_handler=handler).build()


# --- ADS publishing (dirctl) ---------------------------------------------

def _run_dirctl(args: list[str], *, ads_url: str | None) -> subprocess.CompletedProcess[str]:
    server = ads_url or env_ads_addr()
    cmd = [os.getenv("DIRCTL_BIN", "dirctl"), "--server-addr", server, *args]
    try:
        done = subprocess.run(cmd, text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("dirctl is not installed or not on PATH.") from exc
    if done.returncode != 0:
        raise RuntimeError(f"dirctl failed: {' '.join(cmd)}\n{done.stderr.strip() or done.stdout.strip()}")
    return done


def build_ads_record(card: Any) -> dict[str, Any]:
    """Minimal OASF record embedding the agent card.

    NOTE: for production-grade conversion use the canonical converter
    (agent_directory_service.services.directory.card_oasf_converter) or publish
    via `python -m agent_directory_service.scripts.publish_agents --card ...`.
    This minimal builder covers the common single-skill case so the template is
    self-contained.
    """
    url = getattr(card, "url", "") or ""
    created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    skills = getattr(card, "skills", None) or []
    skill_tags = [getattr(s, "id", "") for s in skills if getattr(s, "id", "")]
    display_name = getattr(card, "name", "") or AGENT_NAME
    agent_slug = display_name.lower().replace(" ", "-")
    return {
        "schema_version": "1.0.0",
        # Derive identity from the card so each agent publishes under its own name
        # (this minimal builder is demo-only; emitted agents use ads_utils.publisher).
        "name": (getattr(card, "url", "") or f"https://example.invalid/agents/{agent_slug}"),
        "version": getattr(card, "version", "") or "0.1.0",
        "authors": [display_name],
        "description": getattr(card, "description", "") or "",
        "created_at": created,
        "skills": [{"name": tag} for tag in skill_tags],
        "domains": [],
        "locators": [
            {
                "type": "source_code",
                "urls": [url],
                "annotations": {"a2a.url": url},
            }
        ],
        "modules": [
            {
                "name": "integration/a2a",
                "data": {
                    "card_schema_version": getattr(card, "protocol_version", None) or "0.3.0",
                    "card_data": json.loads(card.model_dump_json()) if hasattr(card, "model_dump_json") else {},
                },
            }
        ],
        "annotations": {"a2a.url": url, "agentic_network.display_name": getattr(card, "name", "")},
    }


def _wait_until_routed(cid: str, *, ads_url: str | None, attempts: int = 10, delay: float = 1.0) -> bool:
    """Poll `dirctl routing list` until `cid` appears (closes the stored-but-not-routed gap)."""
    for _ in range(attempts):
        out = _run_dirctl(["routing", "list"], ads_url=ads_url).stdout
        if cid in out:
            return True
        time.sleep(delay)
    return False


def publish_card_to_ads(card: Any, *, ads_url: str | None = None) -> str:
    record = build_ads_record(card)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        tmp = Path(fh.name)
    try:
        cid = _run_dirctl(["push", str(tmp), "--output", "raw"], ads_url=ads_url).stdout.strip()
        if not cid:
            raise RuntimeError("dirctl push returned no CID.")
        _run_dirctl(["routing", "publish", cid], ads_url=ads_url)
    finally:
        tmp.unlink(missing_ok=True)

    if _wait_until_routed(cid, ads_url=ads_url):
        print(f"[ok] {cid} routed and discoverable.")
    else:
        print(f"[warn] {cid} pushed but not yet visible in routing (stored-but-not-routed risk).")
    return cid


def unpublish_card_from_ads(cid: str | None, *, ads_url: str | None = None) -> None:
    if not cid:
        return
    try:
        _run_dirctl(["routing", "unpublish", cid], ads_url=ads_url)
        print(f"[ok] unpublished {cid}")
    except Exception as exc:  # best-effort cleanup
        print(f"[warn] unpublish failed: {exc}")


# --- CLI ------------------------------------------------------------------

def _add_serve_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", default=env_model())
    p.add_argument("--host", default=env_host())
    p.add_argument("--port", type=int, default=env_port())
    p.add_argument("--card", type=Path, default=DEFAULT_CARD_PATH)
    p.add_argument("--advertise-url", default=env_advertise_url())
    p.add_argument("--publish-ads", action="store_true", help="Publish the card to ADS on startup.")
    p.add_argument("--ads-url", default=env_ads_addr())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Skeleton A2A agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="Validate tool schema/function alignment and exit.")

    serve = sub.add_parser("serve-a2a", help="Run the A2A HTTP server (demo tools).")
    _add_serve_args(serve)

    # The end-goal entry point: serve an LLM loop that wraps an external endpoint.
    wrap = sub.add_parser(
        "serve-wrapper",
        help="Run the A2A server as an LLM wrapper over an external endpoint.",
    )
    _add_serve_args(wrap)
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


def _serve(args: argparse.Namespace, spec: Any = None) -> None:
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

    cid: str | None = None
    if args.publish_ads:
        cid = publish_card_to_ads(card, ads_url=args.ads_url)
        atexit.register(lambda: unpublish_card_from_ads(cid, ads_url=args.ads_url))

    print(f"Serving {getattr(card, 'name', AGENT_NAME)} on {args.host}:{args.port} (url={getattr(card, 'url', '')})")
    try:
        uvicorn.run(create_app(card, model=args.model, spec=spec), host=args.host, port=args.port)
    finally:
        if cid:
            unpublish_card_from_ads(cid, ads_url=args.ads_url)


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "check":
        validate_tool_registry()
        print("OK: tool schemas and functions are aligned.")
        return

    if args.command == "serve-a2a":
        _serve(args, spec=None)
        return

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
