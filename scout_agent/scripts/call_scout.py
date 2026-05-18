"""Call Scout from outside the Scout process via A2A.

Discovers Scout in the directory by name, opens an A2A client over SLIM, and
sends a single test mission. Prints what comes back.

Usage:
    .venv/bin/python scripts/call_scout.py "Your mission here"
    .venv/bin/python scripts/call_scout.py             # uses a default test mission
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from uuid import uuid4

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Suppress a benign SDK warning during cleanup: when transport.close() runs,
# agntcy_app_sdk.transport.slim.session_manager.local_cache_cleanup() looks
# for a session that's already been freed by the underlying Rust SLIM channel
# and logs a WARNING about it. The cleanup is idempotent — the warning is
# harmless. Filter the specific message at the logger level.
class _SuppressSessionNotFound(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "cannot be removed from local cache" not in record.getMessage()


logging.getLogger("agntcy_app_sdk.transport.slim.session_manager").addFilter(
    _SuppressSessionNotFound()
)

from a2a.types import Message, Part, Role, TextPart  # noqa: E402
from agntcy_app_sdk.directory.oasf_converter import oasf_to_agent_card  # noqa: E402

from scout.network.client_factory import make_a2a_client_factory  # noqa: E402
from scout.network.directory import close_directory, open_directory  # noqa: E402


async def main(message_text: str) -> int:
    load_dotenv()
    slim_endpoint = os.environ.get("SLIM_ENDPOINT", "http://localhost:46357")
    directory_endpoint = os.environ.get("DIRECTORY_ENDPOINT", "127.0.0.1:8889")
    secret = os.environ.get(
        "SLIM_SHARED_SECRET",
        "scout-dev-shared-secret-must-be-32+chars-long-PLACEHOLDER",
    )
    target_name = os.environ.get("SCOUT_AGENT_NAME", "Scout")

    # Direct name lookup — search_agent_records does substring match on name.
    dir_client = await open_directory(directory_endpoint)
    try:
        records = await dir_client.search_agent_records(target_name, limit=10)
    finally:
        await close_directory(dir_client)

    candidates: list[tuple[str, "object"]] = []
    for rec in records:
        card = oasf_to_agent_card(rec) if isinstance(rec, dict) else rec
        if card is None or card.name != target_name:
            continue
        created = ""
        if isinstance(rec, dict):
            created = str(rec.get("created_at") or "")
        candidates.append((created, card))

    # Pick the most recently created matching card. Old stale records (from earlier
    # Scout runs that used a different transport URL) sort earlier and are skipped.
    candidates.sort(key=lambda x: x[0], reverse=True)

    # Prefer one whose preferred_transport is slimpatterns (the one the live server
    # is actually listening on). Fall back to most recent.
    target = None
    for _, card in candidates:
        if str(getattr(card, "preferred_transport", "")).lower() in {
            "slimpatterns", "slim",
        }:
            target = card
            break
    if target is None and candidates:
        target = candidates[0][1]

    if target is None:
        names = [
            (oasf_to_agent_card(r) or type("X", (), {"name": "?"})()).name
            for r in records
        ]
        print(
            f"error: no agent named {target_name!r} in directory at "
            f"{directory_endpoint}; saw {names}",
            file=sys.stderr,
        )
        return 1

    print(f"[caller] discovered target: {target.name}", file=sys.stderr)

    factory = make_a2a_client_factory(
        slim_endpoint=slim_endpoint,
        shared_secret=secret,
        client_name="scout_caller",
    )
    a2a_client = await factory.create(target)

    request = Message(
        role=Role.user,
        message_id=str(uuid4()),
        parts=[Part(root=TextPart(text=message_text))],
    )
    print("[caller] sending message; awaiting reply...", file=sys.stderr)
    chunks: list[str] = []
    try:
        async for event in a2a_client.send_message(request=request):
            if isinstance(event, Message):
                for part in event.parts:
                    root = getattr(part, "root", part)
                    text = getattr(root, "text", None)
                    if text:
                        chunks.append(text)
                        print(text)
    finally:
        # Explicitly close the SLIM transport. Without this, the Rust-backed
        # SLIM channel is GC'd in the wrong order at interpreter shutdown and
        # produces a Segmentation fault (the response is already returned by
        # the time this happens, but it makes the exit code dirty).
        transport = getattr(a2a_client, "_transport", None)
        if transport is not None and hasattr(transport, "close"):
            try:
                close_result = transport.close()
                if asyncio.iscoroutine(close_result):
                    await close_result
            except Exception as e:
                print(f"[caller] transport close warning: {e}", file=sys.stderr)
    if not chunks:
        print("[caller] (no text response received)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    msg = (
        " ".join(sys.argv[1:])
        if len(sys.argv) > 1
        else "Brief test mission: in 2-3 sentences, what's notable about the Bab el-Mandeb Strait this month?"
    )
    sys.exit(asyncio.run(main(msg)))
