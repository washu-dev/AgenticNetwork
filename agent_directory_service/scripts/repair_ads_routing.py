from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from agent_directory_service.services.directory.backends import _cids_from_routing_output


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair stored ADS records that are not routed.")
    parser.add_argument(
        "--server-addr",
        default=os.getenv("ADS_SERVER_ADDR") or os.getenv("ADS_URL") or "localhost:8888",
    )
    parser.add_argument("--cid", action="append", help="CID to re-publish. May be repeated.")
    parser.add_argument(
        "--all-storage",
        action="store_true",
        help="Re-publish all CIDs returned by dirctl search.",
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--poll", type=float, default=0.25)
    parser.add_argument(
        "--restart-daemon-if-stuck",
        action="store_true",
        help="If routing verification fails, run dirctl daemon stop/start and re-check.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    cids = list(dict.fromkeys(args.cid or []))
    if args.all_storage:
        cids.extend(cid for cid in _stored_cids(args.server_addr) if cid not in cids)
    if not cids:
        print("No CIDs supplied. Use --cid <cid> or --all-storage.", file=sys.stderr)
        return 2
    failures = 0
    for cid in cids:
        if not repair_cid(
            cid,
            server_addr=args.server_addr,
            timeout=args.timeout,
            poll=args.poll,
            restart_daemon_if_stuck=args.restart_daemon_if_stuck,
        ):
            failures += 1
    return 1 if failures else 0


def repair_cid(
    cid: str,
    *,
    server_addr: str,
    timeout: float,
    poll: float,
    restart_daemon_if_stuck: bool = False,
) -> bool:
    print(f"Re-publishing {cid} to ADS routing index at {server_addr}")
    publish = _run(["dirctl", "--server-addr", server_addr, "routing", "publish", cid, "--output", "json"])
    if publish.returncode != 0:
        print(f"  publish failed: stdout={publish.stdout!r} stderr={publish.stderr!r}", file=sys.stderr)
        return False
    if _wait_until_routed(cid, server_addr=server_addr, timeout=timeout, poll=poll):
        print(f"  routed: {cid}")
        return True
    print(f"  not routed after {timeout:.1f}s", file=sys.stderr)
    if not restart_daemon_if_stuck:
        return False
    print("  restarting dirctl daemon because --restart-daemon-if-stuck was supplied")
    _run(["dirctl", "daemon", "stop"])
    if not _start_daemon():
        print("  daemon did not become healthy after restart", file=sys.stderr)
        return False
    if _wait_until_routed(cid, server_addr=server_addr, timeout=timeout, poll=poll):
        print(f"  routed after daemon restart: {cid}")
        return True
    print(f"  still not routed after daemon restart: {cid}", file=sys.stderr)
    return False


def _stored_cids(server_addr: str) -> list[str]:
    result = _run(["dirctl", "--server-addr", server_addr, "search", "--output", "json"])
    if result.returncode != 0:
        print(f"dirctl search failed: {result.stderr}", file=sys.stderr)
        return []
    try:
        return _cids_from_routing_output(result.stdout)
    except Exception as exc:
        print(f"Could not parse dirctl search output: {exc}", file=sys.stderr)
        return []


def _wait_until_routed(cid: str, *, server_addr: str, timeout: float, poll: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        result = _run(
            ["dirctl", "--server-addr", server_addr, "routing", "list", "--cid", cid, "--output", "json"]
        )
        if result.returncode == 0 and cid in _cids_from_routing_output(result.stdout):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(poll, 0.01))


def _start_daemon() -> bool:
    log_path = Path("agent_directory_service/ads_daemon_repair.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        subprocess.Popen(
            ["dirctl", "daemon", "start"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        status = _run(["dirctl", "daemon", "status"])
        if _daemon_status_is_running(status):
            return True
        time.sleep(0.5)
    return False


def _daemon_status_is_running(status: subprocess.CompletedProcess[str]) -> bool:
    text = (status.stdout + status.stderr).lower()
    return status.returncode == 0 and "daemon is running" in text and "not running" not in text


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
