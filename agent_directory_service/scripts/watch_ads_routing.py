from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, TextIO

from agent_directory_service.services.directory.backends import _cids_from_routing_output


DEFAULT_DAEMON_CONFIG = Path("agent_directory_service/configs/ads_daemon_fast_routing.yaml")
DEFAULT_DAEMON_LOG_FILE = Path("agent_directory_service/ads_daemon_repair.log")
PUBLISHED = "RECORD_PUBLISHED"
UNPUBLISHED = "RECORD_UNPUBLISHED"
CID_KEYS = ("cid", "record_cid", "recordCid", "resource_id", "resourceId")


@dataclass(frozen=True)
class RoutingEvent:
    event_type: str
    cid: str


@dataclass(frozen=True)
class PendingEvent:
    event: RoutingEvent
    due_at: float


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch ADS routing events and repair published CIDs that remain absent from routing list."
    )
    parser.add_argument(
        "--server-addr",
        default=os.getenv("ADS_SERVER_ADDR") or os.getenv("ADS_URL") or "localhost:8888",
        help="ADS server address to watch.",
    )
    parser.add_argument("--debounce-seconds", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--poll", type=float, default=0.25)
    parser.add_argument(
        "--restart-daemon-if-stuck",
        action="store_true",
        help="Restart the local dirctl daemon if a published CID still is not routed after one retry.",
    )
    parser.add_argument(
        "--daemon-config",
        type=Path,
        default=Path(os.getenv("ADS_DAEMON_CONFIG", str(DEFAULT_DAEMON_CONFIG))),
        help="dirctl daemon config to use only when --restart-daemon-if-stuck is supplied.",
    )
    parser.add_argument(
        "--daemon-log-file",
        type=Path,
        default=Path(os.getenv("ADS_DAEMON_REPAIR_LOG_FILE", str(DEFAULT_DAEMON_LOG_FILE))),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    command = [
        "dirctl",
        "--server-addr",
        args.server_addr,
        "events",
        "listen",
        "--types",
        f"{PUBLISHED},{UNPUBLISHED}",
        "--output",
        "jsonl",
    ]
    print(f"Watching ADS routing events at {args.server_addr}", flush=True)
    try:
        with subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        ) as process:
            if process.stdout is None:
                print("dirctl events did not provide stdout", file=sys.stderr)
                return 1
            failures = _consume_event_stream(
                process.stdout,
                server_addr=args.server_addr,
                debounce_seconds=args.debounce_seconds,
                timeout=args.timeout,
                poll=args.poll,
                restart_daemon_if_stuck=args.restart_daemon_if_stuck,
                daemon_config=args.daemon_config,
                daemon_log_file=args.daemon_log_file,
            )
            returncode = process.wait()
    except KeyboardInterrupt:
        print("ADS routing watcher stopped", file=sys.stderr)
        return 130
    return 1 if failures or returncode else 0


def _consume_event_stream(
    stream: TextIO,
    *,
    server_addr: str,
    debounce_seconds: float,
    timeout: float,
    poll: float,
    restart_daemon_if_stuck: bool,
    daemon_config: Path,
    daemon_log_file: Path,
) -> int:
    pending: dict[str, PendingEvent] = {}
    failures = 0
    while True:
        readable, _, _ = select.select([stream], [], [], _select_timeout(pending))
        if readable:
            line = stream.readline()
            if line == "":
                break
            event = _parse_event_line(line)
            if event is None:
                continue
            pending[event.cid] = PendingEvent(
                event=event,
                due_at=time.monotonic() + max(debounce_seconds, 0),
            )
        failures += _process_due_events(
            pending,
            server_addr=server_addr,
            timeout=timeout,
            poll=poll,
            restart_daemon_if_stuck=restart_daemon_if_stuck,
            daemon_config=daemon_config,
            daemon_log_file=daemon_log_file,
        )
    failures += _process_due_events(
        pending,
        server_addr=server_addr,
        timeout=timeout,
        poll=poll,
        restart_daemon_if_stuck=restart_daemon_if_stuck,
        daemon_config=daemon_config,
        daemon_log_file=daemon_log_file,
        force=True,
    )
    return failures


def _select_timeout(pending: dict[str, PendingEvent]) -> float:
    if not pending:
        return 0.5
    next_due = min(item.due_at for item in pending.values())
    return max(next_due - time.monotonic(), 0)


def _process_due_events(
    pending: dict[str, PendingEvent],
    *,
    server_addr: str,
    timeout: float,
    poll: float,
    restart_daemon_if_stuck: bool,
    daemon_config: Path,
    daemon_log_file: Path,
    force: bool = False,
) -> int:
    now = time.monotonic()
    due_cids = [cid for cid, item in pending.items() if force or item.due_at <= now]
    failures = 0
    for cid in due_cids:
        item = pending.pop(cid)
        if item.event.event_type == UNPUBLISHED:
            print(f"routing watcher observed unpublish cid={cid}; no repair needed", flush=True)
            continue
        if not _repair_published_cid(
            cid,
            server_addr=server_addr,
            timeout=timeout,
            poll=poll,
            restart_daemon_if_stuck=restart_daemon_if_stuck,
            daemon_config=daemon_config,
            daemon_log_file=daemon_log_file,
        ):
            failures += 1
    return failures


def _parse_event_line(line: str) -> RoutingEvent | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    event_type = _normalize_event_type(
        _first_string(data, ("type", "event_type", "eventType"))
    )
    if event_type not in {PUBLISHED, UNPUBLISHED}:
        return None
    cid = _find_cid(data)
    if cid is None:
        return None
    return RoutingEvent(event_type=event_type, cid=cid)


def _normalize_event_type(raw: str | None) -> str | None:
    if raw is None:
        return None
    return raw.removeprefix("EVENT_TYPE_")


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def _find_cid(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in CID_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for child in value.values():
            candidate = _find_cid(child)
            if candidate:
                return candidate
    if isinstance(value, list):
        for child in value:
            candidate = _find_cid(child)
            if candidate:
                return candidate
    return None


def _repair_published_cid(
    cid: str,
    *,
    server_addr: str,
    timeout: float,
    poll: float,
    restart_daemon_if_stuck: bool,
    daemon_config: Path,
    daemon_log_file: Path,
) -> bool:
    if _cid_is_routed(cid, server_addr):
        print(f"routing watcher cid already routed: {cid}", flush=True)
        return True

    print(f"routing watcher retrying publish for missing cid={cid}", flush=True)
    publish = _run(
        ["dirctl", "--server-addr", server_addr, "routing", "publish", cid, "--output", "json"],
        allow_fail=True,
    )
    if publish.returncode != 0:
        print(
            f"routing watcher publish failed cid={cid} "
            f"stdout={publish.stdout.strip()!r} stderr={publish.stderr.strip()!r}",
            file=sys.stderr,
        )
        return False
    if _wait_until_routed(cid, server_addr=server_addr, timeout=timeout, poll=poll):
        print(f"routing watcher routed cid={cid}", flush=True)
        return True

    print(f"routing watcher cid still missing after {timeout:.1f}s: {cid}", file=sys.stderr)
    if not restart_daemon_if_stuck:
        print("routing watcher will not restart daemon without --restart-daemon-if-stuck", file=sys.stderr)
        return False

    print("routing watcher restarting local dirctl daemon because --restart-daemon-if-stuck was supplied")
    if not _restart_daemon(daemon_config, daemon_log_file):
        print("routing watcher daemon did not become healthy after restart", file=sys.stderr)
        return False
    if _wait_until_routed(cid, server_addr=server_addr, timeout=timeout, poll=poll):
        print(f"routing watcher routed after daemon restart cid={cid}", flush=True)
        return True
    print(f"routing watcher still missing after daemon restart cid={cid}", file=sys.stderr)
    return False


def _wait_until_routed(cid: str, *, server_addr: str, timeout: float, poll: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        if _cid_is_routed(cid, server_addr):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(poll, 0.01))


def _cid_is_routed(cid: str, server_addr: str) -> bool:
    result = _run(
        ["dirctl", "--server-addr", server_addr, "routing", "list", "--cid", cid, "--output", "json"],
        allow_fail=True,
    )
    if result.returncode != 0:
        return False
    return cid in _cids_from_routing_output(result.stdout)


def _restart_daemon(daemon_config: Path, log_path: Path) -> bool:
    if not daemon_config.exists():
        print(f"routing watcher daemon config does not exist: {daemon_config}", file=sys.stderr)
        return False
    _run(["dirctl", "daemon", "stop"], allow_fail=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        subprocess.Popen(
            ["dirctl", "daemon", "start", "--config", str(daemon_config)],
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
