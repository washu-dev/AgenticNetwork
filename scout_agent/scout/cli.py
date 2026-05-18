import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from scout.agent import ScoutSession, run_mission
from scout.capabilities.base import CapabilityRegistry
from scout.capabilities.brave import BraveNewsSearch, BraveWebSearch
from scout.config import Settings


# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------


def _build_brave_registry(brave_key: str, settings: Settings) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(BraveWebSearch(brave_key, timeout=settings.brave_timeout))
    reg.register(BraveNewsSearch(brave_key, timeout=settings.brave_timeout))
    return reg


def _resolve_settings_common(
    *,
    model: str | None,
    max_iterations: int | None,
    max_tool_calls: int | None,
) -> Settings:
    s = Settings()
    if model:
        s.model = model
    if max_iterations is not None:
        s.max_iterations = max_iterations
    if max_tool_calls is not None:
        s.max_tool_calls = max_tool_calls
    return s


def _load_keys() -> tuple[str | None, str | None, str | None]:
    load_dotenv()
    return (
        os.environ.get("OPENAI_API_KEY"),
        os.environ.get("BRAVE_API_KEY"),
        os.environ.get("SLIM_SHARED_SECRET"),
    )


# ------------------------------------------------------------------
# Legacy (non-network) parser & flow — preserves Phase 1 behavior
# ------------------------------------------------------------------


def _legacy_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scout",
        description="OSINT research & trend analysis agent (Phase 1 standalone mode).",
    )
    p.add_argument(
        "mission",
        nargs="?",
        help="Mission text for one-shot mode. Omit to enter interactive REPL.",
    )
    p.add_argument("--mission-file", help="Read mission from file (one-shot).")
    p.add_argument("--output", help="Write the final reply to this path (one-shot only).")
    p.add_argument("--model", default=None, help="Override the OpenAI model id.")
    p.add_argument("--max-iterations", type=int, default=None)
    p.add_argument("--max-tool-calls", type=int, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _resolve_one_shot_mission(args: argparse.Namespace) -> str | None:
    if args.mission_file:
        return Path(args.mission_file).read_text(encoding="utf-8").strip()
    if args.mission == "-":
        return sys.stdin.read().strip()
    if args.mission:
        return args.mission.strip()
    if not sys.stdin.isatty():  # piped input
        return sys.stdin.read().strip()
    return None


async def _run_one_shot(
    mission: str,
    settings: Settings,
    output: str | None,
    verbose: bool,
    openai_key: str,
    brave_key: str,
) -> int:
    if not mission:
        print("error: empty mission", file=sys.stderr)
        return 2
    registry = _build_brave_registry(brave_key, settings)
    client = AsyncOpenAI(api_key=openai_key)
    reply = await run_mission(mission, registry, settings, client=client, verbose=verbose)
    if output:
        Path(output).write_text(reply, encoding="utf-8")
        if verbose:
            print(f"[scout] wrote {output}", file=sys.stderr)
    else:
        print(reply)
    return 0


async def _run_repl_loop(
    *,
    registry: CapabilityRegistry,
    settings: Settings,
    openai_client: AsyncOpenAI,
    verbose: bool,
    banner_extra: str = "",
) -> int:
    """Reusable REPL — works for both the legacy non-network mode and `serve` mode."""
    session = ScoutSession(
        registry=registry, settings=settings, client=openai_client, verbose=verbose
    )

    print(
        f"Scout REPL — model={settings.model}, "
        f"per-turn budget: {settings.max_iterations} iters / "
        f"{settings.max_tool_calls} tool calls."
        + (f"\n{banner_extra}" if banner_extra else "")
        + "\nType your research question and press Enter. Ctrl-D or Ctrl-C to exit.\n",
        file=sys.stderr,
    )

    while True:
        try:
            user_input = await asyncio.to_thread(input, "you> ")
        except EOFError:
            print(file=sys.stderr)
            return 0
        except KeyboardInterrupt:
            print("\n(use Ctrl-D to exit)", file=sys.stderr)
            continue

        text = user_input.strip()
        if not text:
            continue

        try:
            reply = await session.turn(text)
        except KeyboardInterrupt:
            print("\n[scout] turn cancelled", file=sys.stderr)
            while session.messages and session.messages[-1].get("role") in {
                "assistant", "tool", "user",
            }:
                last = session.messages.pop()
                if last.get("role") == "user" and last.get("content") == text:
                    break
            continue
        except Exception as e:
            print(f"[scout] turn failed: {e}", file=sys.stderr)
            continue

        print(f"\nscout> {reply}\n")


async def _run_legacy(args: argparse.Namespace) -> int:
    openai_key, brave_key, _secret = _load_keys()
    if not openai_key:
        print("error: OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    if not brave_key:
        print("error: BRAVE_API_KEY not set", file=sys.stderr)
        return 2

    settings = _resolve_settings_common(
        model=args.model,
        max_iterations=args.max_iterations,
        max_tool_calls=args.max_tool_calls,
    )

    one_shot = _resolve_one_shot_mission(args)
    if one_shot is not None:
        return await _run_one_shot(
            one_shot, settings, args.output, args.verbose, openai_key, brave_key
        )

    registry = _build_brave_registry(brave_key, settings)
    client = AsyncOpenAI(api_key=openai_key)
    return await _run_repl_loop(
        registry=registry,
        settings=settings,
        openai_client=client,
        verbose=args.verbose,
    )


# ------------------------------------------------------------------
# `scout serve` parser & flow — Phase 2 network mode
# ------------------------------------------------------------------


def _serve_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scout serve",
        description=(
            "Run Scout as a node on the AGNTCY/A2A network: publish self to the "
            "directory, accept incoming A2A calls, optionally also run a local REPL."
        ),
    )
    p.add_argument(
        "--no-repl",
        action="store_true",
        help="Don't start the foreground REPL; just serve A2A in the background.",
    )
    p.add_argument(
        "--directory-endpoint", default=None, help="Override DIRECTORY_ENDPOINT env."
    )
    p.add_argument(
        "--http-host",
        default=None,
        help="Bind address for the JSONRPC server (default 0.0.0.0). Use 127.0.0.1 to be host-only.",
    )
    p.add_argument(
        "--http-port", type=int, default=None, help="Port for the JSONRPC server."
    )
    p.add_argument(
        "--public-host",
        default=None,
        help=(
            "Externally-reachable hostname/IP we put in the AgentCard URL. "
            "Set to your LAN IP if peers run on other machines (otherwise they "
            "see 127.0.0.1 and can't reach you)."
        ),
    )
    p.add_argument(
        "--peer-skill",
        action="append",
        default=None,
        help=(
            "OASF skill id (or substring of agent name) to look for at startup. "
            "Repeat for multiple. Empty = broad search. Only used when "
            "--enable-peer-discovery is set."
        ),
    )
    p.add_argument(
        "--enable-peer-discovery",
        action="store_true",
        default=None,
        help=(
            "Discover other agents in the directory and register them as tools, "
            "so Scout can fan out and call peers from its ReAct loop. Default OFF: "
            "Scout still publishes itself and answers incoming A2A calls (acts as "
            "a tool for others), but does not initiate calls to peers. Can also "
            "be enabled via SCOUT_ENABLE_PEER_DISCOVERY=1."
        ),
    )
    p.add_argument("--model", default=None, help="Override the OpenAI model id.")
    p.add_argument("--max-iterations", type=int, default=None)
    p.add_argument("--max-tool-calls", type=int, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def _run_serve(args: argparse.Namespace) -> int:
    # Lazy imports so legacy mode doesn't need agntcy/a2a installed at import time.
    from scout.network.card import build_scout_card, build_scout_oasf_record
    from scout.network.directory import (
        cleanup_records_by_name,
        close_directory,
        open_directory,
        publish_self,
        unpublish_cid,
    )
    from scout.network.executor import ScoutAgentExecutor
    from scout.network.server import start_a2a_server, stop_a2a_server

    openai_key, brave_key, _ = _load_keys()
    if not openai_key:
        print("error: OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    if not brave_key:
        print("error: BRAVE_API_KEY not set", file=sys.stderr)
        return 2

    settings = _resolve_settings_common(
        model=args.model,
        max_iterations=args.max_iterations,
        max_tool_calls=args.max_tool_calls,
    )
    if args.directory_endpoint:
        settings.directory_endpoint = args.directory_endpoint
    if args.http_host:
        settings.http_host = args.http_host
    if args.http_port:
        settings.http_port = args.http_port
    if args.public_host:
        settings.public_host = args.public_host
    if args.peer_skill:
        settings.peer_skill_filters = args.peer_skill

    peer_discovery_enabled = bool(args.enable_peer_discovery) or (
        os.environ.get("SCOUT_ENABLE_PEER_DISCOVERY", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    # 1. Brave capabilities
    registry = _build_brave_registry(brave_key, settings)

    # 2. Directory: always connect (we need it to publish ourselves), but only
    #    discover + register peers as tools when fan-out is explicitly enabled.
    #    With discovery off, Scout still answers incoming A2A calls but won't
    #    initiate calls to other agents from its own ReAct loop.
    print(
        f"[scout] connecting to directory at {settings.directory_endpoint}...",
        file=sys.stderr,
    )
    dir_client = await open_directory(settings.directory_endpoint)

    if peer_discovery_enabled:
        from scout.capabilities.a2a_peer import A2APeerCapability
        from scout.network.client_factory import make_a2a_client_factory
        from scout.network.directory import discover_peers

        try:
            peer_cards = await discover_peers(
                dir_client,
                skill_filters=settings.peer_skill_filters or None,
                exclude_names={settings.agent_name},
            )
        except Exception as e:
            print(f"[scout] peer discovery failed: {e}", file=sys.stderr)
            peer_cards = []

        print(
            f"[scout] discovered {len(peer_cards)} peer(s): "
            f"{[c.name for c in peer_cards]}",
            file=sys.stderr,
        )

        client_factory = make_a2a_client_factory()
        for peer in peer_cards:
            try:
                registry.register(A2APeerCapability(peer, client_factory))
            except ValueError as e:  # duplicate name
                print(f"[scout] skip peer {peer.name}: {e}", file=sys.stderr)
    else:
        peer_cards = []
        print(
            "[scout] peer discovery disabled (tool-only mode); "
            "pass --enable-peer-discovery to fan out to peers",
            file=sys.stderr,
        )

    # 3. Build server card. We listen on http_host:http_port but advertise
    #    public_host:http_port so peers on other machines can reach us.
    bind_endpoint = f"http://{settings.http_host}:{settings.http_port}/"
    card = build_scout_card(
        http_endpoint=bind_endpoint, agent_name=settings.agent_name
    )

    openai_client = AsyncOpenAI(api_key=openai_key)
    executor = ScoutAgentExecutor(
        registry, settings, openai_client, verbose=args.verbose
    )

    factory = await start_a2a_server(card=card, executor=executor)

    # 4. Cleanup any stale records of OUR name from previous runs (crashed exits,
    #    URL changes, etc.) before publishing the fresh one.
    try:
        n = await cleanup_records_by_name(dir_client, settings.agent_name)
        if n:
            print(f"[scout] cleaned up {n} stale record(s)", file=sys.stderr)
    except Exception as e:
        print(f"[scout] startup cleanup failed: {e}", file=sys.stderr)

    # 5. Re-stamp the card's URL with the public host before publishing — peers
    #    will read from the directory and need a routable URL, not 0.0.0.0.
    #    Then build the OASF record (with proper skill IDs) and push that.
    public_card = build_scout_card(
        http_endpoint=settings.public_endpoint, agent_name=settings.agent_name
    )
    oasf_record = build_scout_oasf_record(public_card)
    cid: str | None = None
    try:
        cid = await publish_self(dir_client, oasf_record)
        print(f"[scout] published to directory; cid={cid}", file=sys.stderr)
    except Exception as e:
        print(f"[scout] publish_self failed: {e}", file=sys.stderr)

    print(
        f"[scout] serving A2A as '{settings.agent_name}' "
        f"(bind={bind_endpoint}, advertised={settings.public_endpoint})",
        file=sys.stderr,
    )

    # 6. Foreground REPL or block on signal
    rc = 0
    try:
        if args.no_repl:
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop_event.set)
                except NotImplementedError:
                    pass  # Windows
            print("[scout] no-repl mode; press Ctrl-C to exit", file=sys.stderr)
            await stop_event.wait()
        else:
            rc = await _run_repl_loop(
                registry=registry,
                settings=settings,
                openai_client=openai_client,
                verbose=args.verbose,
                banner_extra=(
                    f"[networked: {len(peer_cards)} peer(s) discovered, "
                    f"serving as '{settings.agent_name}']"
                ),
            )
    finally:
        print("[scout] shutting down...", file=sys.stderr)
        # Unpublish + delete the record we created this run so other peers
        # don't keep trying to call a dead Scout. Crashes won't get this
        # cleanup — that's why startup also runs cleanup_records_by_name.
        if cid is not None:
            try:
                await unpublish_cid(dir_client, cid)
            except Exception as e:
                print(f"[scout] shutdown unpublish failed: {e}", file=sys.stderr)
        await stop_a2a_server(factory)
        await close_directory(dir_client)
    return rc


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "serve":
        args = _serve_parser().parse_args(raw[1:])
        try:
            return asyncio.run(_run_serve(args))
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130

    args = _legacy_parser().parse_args(raw)
    try:
        return asyncio.run(_run_legacy(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
