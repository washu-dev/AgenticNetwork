"""Equivalent of `dirctl --server-addr <DIR> routing list --output json`.

The Python SDK's `Client.list()` returns 0 records against this remote dir even
when records exist (suspected SDK bug). Easiest reliable workaround: shell out
to the official dirctl docker image. This keeps verification self-contained
without needing access to the dir host.

Usage:
    .venv/bin/python scripts/routing_list.py             # all records
    .venv/bin/python scripts/routing_list.py --skill "natural_language_processing"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from dotenv import load_dotenv

DIRCTL_IMAGE = "ghcr.io/agntcy/dir-ctl:v1.2.0"


def main() -> int:
    load_dotenv()
    endpoint = os.environ.get("DIRECTORY_ENDPOINT", "127.0.0.1:8889")

    p = argparse.ArgumentParser()
    p.add_argument("--skill", action="append", default=[])
    p.add_argument("--locator", action="append", default=[])
    p.add_argument("--cid", default=None)
    args = p.parse_args()

    cmd = [
        "docker", "run", "--rm", "--network=host",
        DIRCTL_IMAGE,
        "--server-addr", endpoint,
        "routing", "list", "--output", "json",
    ]
    for s in args.skill:
        cmd += ["--skill", s]
    for loc in args.locator:
        cmd += ["--locator", loc]
    if args.cid:
        cmd += ["--cid", args.cid]

    print(f"[routing-list] {' '.join(cmd)}", file=sys.stderr)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        print("error: docker not found on PATH", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("error: dirctl call timed out", file=sys.stderr)
        return 2

    if out.returncode != 0:
        print(out.stderr, file=sys.stderr)
        return out.returncode

    raw = out.stdout.strip()
    if not raw:
        print("(no records)")
        return 0

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(raw)
        return 0

    if not isinstance(data, list):
        data = [data]

    print(f"{len(data)} record(s):")
    for i, rec in enumerate(data, 1):
        cid = rec.get("record_ref", {}).get("cid", "?")
        labels = rec.get("labels") or []
        print(f"\n  [{i}] cid={cid}")
        for label in sorted(labels):
            print(f"      {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
