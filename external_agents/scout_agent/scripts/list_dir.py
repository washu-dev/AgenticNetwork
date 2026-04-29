"""List agents in a directory (verify connectivity & see what's published).

Usage:
    .venv/bin/python scripts/list_dir.py                # list ALL A2A agents
    .venv/bin/python scripts/list_dir.py "term"         # additionally filter by name substring

Reads DIRECTORY_ENDPOINT from .env (default 127.0.0.1:8889). Uses the broad
enumeration path (search_records with MODULE_NAME=integration/a2a) so it works
without a query — useful for "show me everything in the dir".
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scout.network  # noqa: F401  -- installs SDK warning filter

from scout.network.directory import (  # noqa: E402
    close_directory,
    discover_peers,
    open_directory,
)


async def main(name_filter: str | None) -> int:
    load_dotenv()
    endpoint = os.environ.get("DIRECTORY_ENDPOINT", "127.0.0.1:8889")
    print(f"[list-dir] connecting to {endpoint}", file=sys.stderr)

    dc = await open_directory(endpoint)
    try:
        cards = await discover_peers(dc, exclude_names=set())
    finally:
        await close_directory(dc)

    if name_filter:
        cards = [c for c in cards if name_filter.lower() in (c.name or "").lower()]
        print(f"[list-dir] filter={name_filter!r}", file=sys.stderr)

    if not cards:
        print("(no records)")
        return 0

    print(f"{len(cards)} record(s):")
    for i, card in enumerate(cards, 1):
        ifaces = []
        if card.additional_interfaces:
            ifaces = [(x.transport, x.url) for x in card.additional_interfaces]
        skill_ids = [s.id for s in (card.skills or [])]
        print(f"  [{i}] name={card.name!r} version={card.version}")
        print(f"      preferred_transport={card.preferred_transport}")
        print(f"      skills: {skill_ids}")
        if ifaces:
            for t, u in ifaces:
                print(f"      iface: transport={t} url={u}")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(asyncio.run(main(arg)))
