from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Sequence

from agentnet.directory.backends import (
    ADS_DAEMON_REPAIR_LOG,
    _cids_from_routing_output,
    _record_from_pull_output,
)


@dataclass(frozen=True)
class StoredAgentRecord:
    cid: str
    agent_key: str
    agent_id: str
    name: str
    endpoint: str
    created_at: str


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Force the latest stored ADS record for each agent into the routing index. "
            "Run this on the ADS host when dirctl search shows records but routing list is empty."
        )
    )
    parser.add_argument(
        "--server-addr",
        default=os.getenv("ADS_SERVER_ADDR") or os.getenv("ADS_URL") or "localhost:8888",
        help="ADS server address to repair.",
    )
    parser.add_argument(
        "--cid",
        action="append",
        help="Specific stored CID to consider. May be repeated. Defaults to all dirctl search CIDs.",
    )
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--poll", type=float, default=0.25)
    parser.add_argument(
        "--restart-daemon-if-stuck",
        action="store_true",
        help="Restart the local dirctl daemon if routing publish is accepted but not indexed.",
    )
    parser.add_argument(
        "--delete-superseded",
        action="store_true",
        help="Delete older duplicate records for the same agent after routing the newest one.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected active records without publishing or deleting.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    cids = list(dict.fromkeys(args.cid or _stored_cids(args.server_addr)))
    if not cids:
        print(f"No stored records found at {args.server_addr}.")
        return 0

    records: list[StoredAgentRecord] = []
    unpullable: list[str] = []
    for cid in cids:
        record = _try_pull_agent_record(cid, args.server_addr)
        if record is None:
            unpullable.append(cid)
            continue
        records.append(record)
    if unpullable:
        print("Skipped unpullable stale search records:", file=sys.stderr)
        for cid in unpullable:
            print(f"  {cid}", file=sys.stderr)
    if not records:
        if args.delete_superseded:
            _delete_unpullable(unpullable, args.server_addr)
        print("No pullable stored agent records found.", file=sys.stderr)
        return 1

    selected, superseded = _select_latest_per_agent(records)

    print(f"ADS server: {args.server_addr}")
    print("Selected active records:")
    for record in selected:
        print(
            f"  {record.cid}  agent={record.agent_id}  name={record.name}  "
            f"endpoint={record.endpoint}  created_at={record.created_at}"
        )
    if superseded:
        print("Superseded records:")
        for record in superseded:
            print(
                f"  {record.cid}  agent={record.agent_id}  name={record.name}  "
                f"endpoint={record.endpoint}  created_at={record.created_at}"
            )

    if args.dry_run:
        return 0

    failures = 0
    for record in selected:
        if not _force_route(
            record,
            server_addr=args.server_addr,
            timeout=args.timeout,
            poll=args.poll,
            restart_daemon_if_stuck=args.restart_daemon_if_stuck,
        ):
            failures += 1

    if args.delete_superseded:
        _delete_unpullable(unpullable, args.server_addr)
        for record in superseded:
            _run(
                [
                    "dirctl",
                    "--server-addr",
                    args.server_addr,
                    "routing",
                    "unpublish",
                    record.cid,
                    "--output",
                    "json",
                ],
                allow_fail=True,
            )
            _run(
                [
                    "dirctl",
                    "--server-addr",
                    args.server_addr,
                    "delete",
                    record.cid,
                    "--output",
                    "json",
                ],
                allow_fail=True,
            )

    routed = _routed_cids(args.server_addr)
    missing = [record.cid for record in selected if record.cid not in routed]
    if missing:
        print(f"Still missing from routing: {missing}", file=sys.stderr)
        failures += len(missing)
    else:
        print("All selected active records are routed.")
    return 1 if failures else 0


def _stored_cids(server_addr: str) -> list[str]:
    result = _run(["dirctl", "--server-addr", server_addr, "search", "--output", "json"])
    return _cids_from_routing_output(result.stdout)


def _routed_cids(server_addr: str) -> list[str]:
    result = _run(["dirctl", "--server-addr", server_addr, "routing", "list", "--output", "json"])
    return _cids_from_routing_output(result.stdout)


def _pull_agent_record(cid: str, server_addr: str) -> StoredAgentRecord:
    result = _run(["dirctl", "--server-addr", server_addr, "pull", cid, "--output", "json"])
    record = _record_from_pull_output(result.stdout)
    annotations = record.get("annotations") if isinstance(record.get("annotations"), dict) else {}
    card = _embedded_agent_card(record)
    agent_id = (
        annotations.get("agentic_network.agent_id")
        or (card or {}).get("name")
        or record.get("name")
        or cid
    )
    name = annotations.get("agentic_network.display_name") or (card or {}).get("name") or str(agent_id)
    endpoint = annotations.get("a2a.url") or (card or {}).get("url") or ""
    created_at = str(record.get("created_at") or "")
    agent_key = f"{agent_id}|{endpoint or name}"
    return StoredAgentRecord(
        cid=cid,
        agent_key=agent_key,
        agent_id=str(agent_id),
        name=str(name),
        endpoint=str(endpoint),
        created_at=created_at,
    )


def _try_pull_agent_record(cid: str, server_addr: str) -> StoredAgentRecord | None:
    try:
        return _pull_agent_record(cid, server_addr)
    except Exception as exc:
        print(f"Could not pull {cid}: {exc}", file=sys.stderr)
        return None


def _delete_unpullable(cids: list[str], server_addr: str) -> None:
    for cid in cids:
        result = _run(
            ["dirctl", "--server-addr", server_addr, "delete", cid, "--output", "json"],
            allow_fail=True,
        )
        if result.returncode == 0:
            print(f"Deleted unpullable stale record: {cid}")
        else:
            print(
                f"Could not delete unpullable stale record {cid}: "
                f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}",
                file=sys.stderr,
            )


def _embedded_agent_card(record: dict[str, Any]) -> dict[str, Any] | None:
    for module in record.get("modules", []):
        if not isinstance(module, dict):
            continue
        data = module.get("data")
        if isinstance(data, dict) and isinstance(data.get("card_data"), dict):
            return data["card_data"]
    return None


def _select_latest_per_agent(
    records: list[StoredAgentRecord],
) -> tuple[list[StoredAgentRecord], list[StoredAgentRecord]]:
    by_key: dict[str, list[StoredAgentRecord]] = {}
    for record in records:
        by_key.setdefault(record.agent_key, []).append(record)

    selected: list[StoredAgentRecord] = []
    superseded: list[StoredAgentRecord] = []
    for group in by_key.values():
        ordered = sorted(group, key=lambda item: (item.created_at, item.cid))
        selected.append(ordered[-1])
        superseded.extend(ordered[:-1])
    return sorted(selected, key=lambda item: item.agent_id), sorted(
        superseded,
        key=lambda item: item.agent_id,
    )


def _force_route(
    record: StoredAgentRecord,
    *,
    server_addr: str,
    timeout: float,
    poll: float,
    restart_daemon_if_stuck: bool,
) -> bool:
    print(f"Publishing routing entry for {record.agent_id}: {record.cid}")
    publish = _run(
        ["dirctl", "--server-addr", server_addr, "routing", "publish", record.cid, "--output", "json"],
        allow_fail=True,
    )
    if publish.returncode != 0:
        print(
            f"  publish failed: stdout={publish.stdout.strip()!r} stderr={publish.stderr.strip()!r}",
            file=sys.stderr,
        )
        return False
    if _wait_until_routed(record.cid, server_addr=server_addr, timeout=timeout, poll=poll):
        print(f"  routed: {record.cid}")
        return True
    print(f"  not routed after {timeout:.1f}s", file=sys.stderr)
    if not restart_daemon_if_stuck:
        print("  retry with --restart-daemon-if-stuck if this ADS host owns the daemon.", file=sys.stderr)
        return False

    print("  restarting local dirctl daemon because --restart-daemon-if-stuck was supplied")
    _run(["dirctl", "daemon", "stop"], allow_fail=True)
    if not _start_daemon():
        print("  daemon did not become healthy after restart", file=sys.stderr)
        return False
    if _wait_until_routed(record.cid, server_addr=server_addr, timeout=timeout, poll=poll):
        print(f"  routed after daemon restart: {record.cid}")
        return True
    print(f"  still not routed after daemon restart: {record.cid}", file=sys.stderr)
    return False


def _wait_until_routed(cid: str, *, server_addr: str, timeout: float, poll: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        if cid in _routed_cids(server_addr):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(poll, 0.01))


def _start_daemon() -> bool:
    log_path = ADS_DAEMON_REPAIR_LOG
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
        status = _run(["dirctl", "daemon", "status"], allow_fail=True)
        if _daemon_status_is_running(status):
            return True
        time.sleep(0.5)
    return False


def _daemon_status_is_running(status: subprocess.CompletedProcess[str]) -> bool:
    text = (status.stdout + status.stderr).lower()
    return status.returncode == 0 and "daemon is running" in text and "not running" not in text


def _run(command: list[str], *, allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0 and not allow_fail:
        raise RuntimeError(
            f"Command failed: {' '.join(command)}\n"
            f"stdout={result.stdout.strip()!r}\nstderr={result.stderr.strip()!r}"
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
