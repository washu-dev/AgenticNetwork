"""File-backed fake of the AGNTCY ``dirctl`` CLI for the native dev stack.

The planner, the registration service and skeleton agents all reach the agent
directory by shelling out to ``dirctl`` (``DIRCTL_BIN``). Pointing every process
at this fake (``devstack/bin/dirctl``) with the same ``FAKE_DIRCTL_STATE`` file
gives them one shared directory without a running ``dir`` daemon.

It models the part of the real directory that bites in practice: a record is
*stored* by ``push`` but only *discoverable* once ``routing publish`` has routed
it. ``routing unpublish`` unroutes (the record stays stored) and ``delete``
removes it. ``FAKE_DIRCTL_ROUTE_DELAY_SECONDS`` delays routing to imitate the
daemon's scheduler and indexer.

Supported commands (every one the repository calls):

    push FILE                      store a record, print its CID
    pull CID                       print a stored record
    delete CID                     remove a record (stored and routed)
    search [filters]               storage search (stored records)
    routing publish CID            route a stored record
    routing unpublish CID          unroute a record
    routing list [--cid] [filters] routed records on this node
    routing search [filters]       network search (this node only)
    routing info                   counts
    events listen [--types T,..]   stream routing events as JSON lines
    version | daemon start|stop|status

Global flags: ``--server-addr`` (accepted and logged, never contacted) and
``--output``/``-o`` (``json``, ``jsonl``, ``raw`` or text when omitted). The
JSON shapes follow the real CLI (``routing list`` returns
``[{"record_ref": {"cid": ...}, "labels": [...]}]``), which is what
``planner_agent/main_agent/directory/backends.py``, ``ads_utils/publisher.py``,
``agent_skeleton/serve.py`` and ``agent_directory_service/scripts`` parse.

Concurrent processes are safe: every command holds an ``fcntl`` lock on
``<state>.lock`` and writes the state file atomically.

Standard library only, Python 3.8+ (the shim runs on whatever ``python3`` is on
PATH).
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

FAKE_VERSION = "v1.2.0-devstack-fake"
STATE_ENV = "FAKE_DIRCTL_STATE"
LOG_ENV = "FAKE_DIRCTL_LOG"
ROUTE_DELAY_ENV = "FAKE_DIRCTL_ROUTE_DELAY_SECONDS"
MAX_EVENTS = 1000

# Flags that take a value, per command: the ones the repository uses. Anything
# else is rejected the way the real CLI rejects an unknown flag, so a caller
# using a flag this fake does not model fails loudly instead of silently getting
# wrong results.
_FILTER_FLAGS = ("--skill", "--domain", "--module", "--locator")
_COMMAND_FLAGS: Dict[Tuple[str, ...], Tuple[str, ...]] = {
    ("push",): (),
    ("pull",): (),
    ("delete",): (),
    ("search",): _FILTER_FLAGS + ("--limit",),
    ("routing", "publish"): (),
    ("routing", "unpublish"): (),
    ("routing", "list"): _FILTER_FLAGS + ("--cid", "--limit"),
    ("routing", "search"): _FILTER_FLAGS + ("--limit",),
    ("routing", "info"): (),
    ("events", "listen"): ("--types",),
    ("version",): (),
    ("daemon", "start"): ("--config",),
    ("daemon", "stop"): (),
    ("daemon", "status"): (),
}
_GROUPS = {"routing", "events", "daemon"}


class DirctlError(Exception):
    """A command failure: printed as ``Error: ...`` on stderr, exit code ``code``."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


# ── Argument parsing ──────────────────────────────────────────────────────────


class Invocation:
    def __init__(self) -> None:
        self.command: Tuple[str, ...] = ()
        self.positionals: List[str] = []
        self.flags: Dict[str, List[str]] = {}
        self.output: Optional[str] = None
        self.server_addr: Optional[str] = None

    def flag(self, name: str) -> Optional[str]:
        values = self.flags.get(name)
        return values[-1] if values else None

    def flag_all(self, name: str) -> List[str]:
        return list(self.flags.get(name, []))


def parse_args(argv: List[str]) -> Invocation:
    inv = Invocation()
    words: List[str] = []
    raw_flags: List[Tuple[str, Optional[str]]] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("-") and arg != "-":
            name, eq, value = arg.partition("=")
            if name in ("-h", "--help"):
                raise DirctlError(_usage(), code=0)
            if not eq:
                value = argv[i + 1] if i + 1 < len(argv) else None
                if value is None:
                    raise DirctlError(f"flag needs an argument: {name}")
                i += 1
            raw_flags.append((name, value))
        else:
            words.append(arg)
        i += 1

    if not words:
        raise DirctlError(_usage(), code=2)
    if words[0] in _GROUPS:
        if len(words) < 2:
            raise DirctlError(f"'{words[0]}' needs a subcommand\n\n{_usage()}", code=2)
        inv.command = (words[0], words[1])
        inv.positionals = words[2:]
    else:
        inv.command = (words[0],)
        inv.positionals = words[1:]
    if inv.command not in _COMMAND_FLAGS:
        raise DirctlError(f"unknown command {' '.join(inv.command)!r} (fake dirctl)\n\n{_usage()}", code=2)

    allowed = _COMMAND_FLAGS[inv.command]
    for name, value in raw_flags:
        if name == "--server-addr":
            inv.server_addr = value
        elif name in ("--output", "-o"):
            inv.output = value
        elif name in allowed:
            inv.flags.setdefault(name, []).append(str(value))
        else:
            raise DirctlError(f"unknown flag: {name} (fake dirctl, command {' '.join(inv.command)!r})", code=2)
    return inv


def _usage() -> str:
    lines = ["Usage: dirctl [--server-addr ADDR] [--output json|jsonl|raw] COMMAND", "", "Commands (fake):"]
    for command, flags in _COMMAND_FLAGS.items():
        lines.append(f"  {' '.join(command):<20} {' '.join(flags)}".rstrip())
    return "\n".join(lines)


# ── State ─────────────────────────────────────────────────────────────────────


def _state_path() -> str:
    path = os.environ.get(STATE_ENV, "").strip()
    if not path:
        raise DirctlError(f"{STATE_ENV} is not set; the fake dirctl needs a state file path.")
    return path


def empty_state() -> Dict[str, Any]:
    return {"version": 1, "records": {}, "events": [], "next_event": 1}


@contextlib.contextmanager
def locked_state(path: str, *, write: bool) -> Iterator[Dict[str, Any]]:
    """Yield the parsed state under an exclusive lock; save it if ``write``."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = _read_state(path)
            yield state
            if write:
                _write_state(path, state)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return empty_state()
    if not raw.strip():
        return empty_state()
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DirctlError(f"fake directory state {path} is not valid JSON: {exc}")
    for key, default in empty_state().items():
        state.setdefault(key, default)
    return state


def _write_state(path: str, state: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".fake-dirctl-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def init_state(path: str) -> None:
    """Create (or empty) a state file. Used by the dev stack and tests."""
    with locked_state(path, write=True) as state:
        state.clear()
        state.update(empty_state())


def _emit_event(state: Dict[str, Any], event_type: str, cid: str) -> None:
    seq = int(state.get("next_event", 1))
    state["next_event"] = seq + 1
    state["events"].append(
        {"id": seq, "type": f"EVENT_TYPE_{event_type}", "resource_id": cid, "timestamp": _now_iso()}
    )
    del state["events"][:-MAX_EVENTS]


# ── Records, labels and matching ──────────────────────────────────────────────


def compute_cid(record: Dict[str, Any]) -> str:
    """Content address of a record: same record, same CID (like the real store)."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = base64.b32encode(hashlib.sha256(canonical).digest()).decode("ascii").lower().rstrip("=")
    return "baeareih" + digest[:51]


def record_labels(record: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    for skill in record.get("skills") or []:
        if isinstance(skill, dict):
            if skill.get("name"):
                labels.append(f"/skills/{skill['name']}")
            if skill.get("id") is not None:
                labels.append(f"/skills/{skill['id']}")
        elif isinstance(skill, str):
            labels.append(f"/skills/{skill}")
    for domain in record.get("domains") or []:
        if isinstance(domain, dict) and (domain.get("name") or domain.get("id") is not None):
            labels.append(f"/domains/{domain.get('name') or domain.get('id')}")
    for module in record.get("modules") or []:
        if isinstance(module, dict) and module.get("name"):
            labels.append(f"/modules/{module['name']}")
    for locator in record.get("locators") or []:
        if isinstance(locator, dict) and locator.get("type"):
            labels.append(f"/locators/{locator['type']}")
    return list(dict.fromkeys(labels))


def _label_matches(labels: List[str], kind: str, pattern: str) -> List[str]:
    """Labels of ``kind`` matching ``pattern``: case-insensitive, ``*``/``?``
    wildcards, and a hierarchical prefix (``nlp`` matches ``nlp/summarization``)."""
    pattern = pattern.strip().lower()
    prefix = f"/{kind}/"
    hits = []
    for label in labels:
        if not label.startswith(prefix):
            continue
        value = label[len(prefix):].lower()
        if fnmatch.fnmatchcase(value, pattern) or value.startswith(pattern.rstrip("/") + "/"):
            hits.append(label)
    return hits


def _filter_matches(entry: Dict[str, Any], inv: Invocation) -> Optional[List[str]]:
    """Matched labels if ``entry`` passes every filter flag, else None.

    Repeated values of one flag are alternatives (OR); different flags must all
    match (AND)."""
    labels = entry["labels"]
    matched: List[str] = []
    for flag, kind in (("--skill", "skills"), ("--domain", "domains"), ("--module", "modules"), ("--locator", "locators")):
        patterns = inv.flag_all(flag)
        if not patterns:
            continue
        hits = [hit for pattern in patterns for hit in _label_matches(labels, kind, pattern)]
        if not hits:
            return None
        matched.extend(hits)
    return list(dict.fromkeys(matched))


def _is_routed(entry: Dict[str, Any], now: float) -> bool:
    visible_at = entry.get("routed_visible_at")
    return visible_at is not None and float(visible_at) <= now


def _int_flag(inv: Invocation, name: str, default: Optional[int]) -> Optional[int]:
    value = inv.flag(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise DirctlError(f"invalid value {value!r} for {name}: expected an integer")


def _limit(items: List[Any], inv: Invocation) -> List[Any]:
    limit = _int_flag(inv, "--limit", None)
    return items if limit is None or limit <= 0 else items[:limit]


def _require_cid(inv: Invocation) -> str:
    if len(inv.positionals) != 1:
        raise DirctlError(f"{' '.join(inv.command)} expects exactly one CID argument")
    return inv.positionals[0]


def _get_entry(state: Dict[str, Any], cid: str) -> Dict[str, Any]:
    entry = state["records"].get(cid)
    if entry is None:
        raise DirctlError(f"record not found: {cid}")
    return entry


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_push(inv: Invocation, path: str) -> str:
    if len(inv.positionals) != 1:
        raise DirctlError("push expects exactly one record file argument")
    source = inv.positionals[0]
    try:
        raw = sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()
    except OSError as exc:
        raise DirctlError(f"failed to read record file {source}: {exc}")
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DirctlError(f"failed to parse record {source}: {exc}")
    if not isinstance(record, dict):
        raise DirctlError("failed to parse record: expected a JSON object")
    cid = compute_cid(record)
    with locked_state(path, write=True) as state:
        entry = state["records"].get(cid)
        if entry is None:
            state["records"][cid] = {
                "record": record,
                "labels": record_labels(record),
                "stored_at": _now_iso(),
                "routed_visible_at": None,
            }
            _emit_event(state, "RECORD_PUSHED", cid)
    if inv.output == "json":
        return json.dumps({"cid": cid})
    return cid


def cmd_pull(inv: Invocation, path: str) -> str:
    cid = _require_cid(inv)
    with locked_state(path, write=False) as state:
        record = _get_entry(state, cid)["record"]
    return json.dumps(record, indent=2 if inv.output not in ("json", "raw") else None)


def cmd_delete(inv: Invocation, path: str) -> str:
    cid = _require_cid(inv)
    with locked_state(path, write=True) as state:
        _get_entry(state, cid)
        del state["records"][cid]
        _emit_event(state, "RECORD_DELETED", cid)
    return json.dumps({"cid": cid, "deleted": True}) if inv.output == "json" else f"Deleted record {cid}"


def cmd_search(inv: Invocation, path: str) -> str:
    with locked_state(path, write=False) as state:
        cids = [cid for cid, entry in state["records"].items() if _filter_matches(entry, inv) is not None]
    cids = _limit(cids, inv)
    if inv.output in ("json", "jsonl"):
        return json.dumps(cids) if inv.output == "json" else "\n".join(json.dumps(c) for c in cids)
    return "\n".join(cids)


def cmd_routing_publish(inv: Invocation, path: str) -> str:
    cid = _require_cid(inv)
    delay = _route_delay()
    with locked_state(path, write=True) as state:
        entry = _get_entry(state, cid)
        if entry.get("routed_visible_at") is None:
            entry["routed_visible_at"] = time.time() + delay
            _emit_event(state, "RECORD_PUBLISHED", cid)
    if inv.output == "json":
        return json.dumps({"cid": cid, "status": "published" if delay <= 0 else "queued"})
    return f"Successfully submitted publication request for record {cid}"


def cmd_routing_unpublish(inv: Invocation, path: str) -> str:
    cid = _require_cid(inv)
    with locked_state(path, write=True) as state:
        entry = _get_entry(state, cid)
        if entry.get("routed_visible_at") is not None:
            entry["routed_visible_at"] = None
            _emit_event(state, "RECORD_UNPUBLISHED", cid)
    if inv.output == "json":
        return json.dumps({"cid": cid, "status": "unpublished"})
    return f"Successfully unpublished record {cid}"


def _routed_items(inv: Invocation, path: str, *, network: bool) -> List[Dict[str, Any]]:
    now = time.time()
    wanted_cid = inv.flag("--cid")
    items: List[Dict[str, Any]] = []
    with locked_state(path, write=False) as state:
        for cid, entry in state["records"].items():
            if not _is_routed(entry, now) or (wanted_cid and cid != wanted_cid):
                continue
            matched = _filter_matches(entry, inv)
            if matched is None:
                continue
            if network:
                items.append(
                    {
                        "record_ref": {"cid": cid},
                        "peer": {"id": "devstack-fake-peer", "addrs": []},
                        "match_count": len(matched),
                        "labels": matched or entry["labels"],
                    }
                )
            else:
                items.append({"record_ref": {"cid": cid}, "labels": entry["labels"]})
    return _limit(items, inv)


def _render_routed(items: List[Dict[str, Any]], inv: Invocation, title: str) -> str:
    if inv.output == "json":
        return json.dumps(items)
    if inv.output == "jsonl":
        return "\n".join(json.dumps(item) for item in items)
    if inv.output == "raw":
        return "\n".join(item["record_ref"]["cid"] for item in items)
    lines = [f"{title}: {len(items)} record(s)"]
    for item in items:
        lines.append(f"  CID: {item['record_ref']['cid']}")
        lines.append(f"    Labels: {', '.join(item['labels']) or '(none)'}")
    return "\n".join(lines)


def cmd_routing_list(inv: Invocation, path: str) -> str:
    return _render_routed(_routed_items(inv, path, network=False), inv, "Local routed records")


def cmd_routing_search(inv: Invocation, path: str) -> str:
    return _render_routed(_routed_items(inv, path, network=True), inv, "Network search results")


def cmd_routing_info(inv: Invocation, path: str) -> str:
    now = time.time()
    label_counts: Dict[str, int] = {}
    routed = pending = 0
    with locked_state(path, write=False) as state:
        stored = len(state["records"])
        for entry in state["records"].values():
            if _is_routed(entry, now):
                routed += 1
                for label in entry["labels"]:
                    label_counts[label] = label_counts.get(label, 0) + 1
            elif entry.get("routed_visible_at") is not None:
                pending += 1
    info = {
        "peer_id": "devstack-fake-peer",
        "stored_records": stored,
        "routed_records": routed,
        "pending_records": pending,
        "labels": label_counts,
        "fake": True,
    }
    if inv.output in ("json", "jsonl", "raw"):
        return json.dumps(info)
    return f"Routed records: {routed}\nPending records: {pending}\nStored records: {stored}"


def cmd_events_listen(inv: Invocation, path: str) -> Optional[str]:
    """Stream new events as JSON lines until interrupted (like the real CLI)."""
    wanted = set()
    for value in inv.flag_all("--types"):
        for item in value.split(","):
            item = item.strip()
            if item:
                wanted.add(item if item.startswith("EVENT_TYPE_") else f"EVENT_TYPE_{item}")
    with locked_state(path, write=False) as state:
        last_seen = int(state.get("next_event", 1)) - 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            with locked_state(path, write=False) as state:
                events = [e for e in state["events"] if int(e["id"]) > last_seen]
            for event in events:
                last_seen = max(last_seen, int(event["id"]))
                if wanted and event["type"] not in wanted:
                    continue
                if inv.output == "json":
                    print(json.dumps(event, indent=2), flush=True)
                elif inv.output == "jsonl":
                    print(json.dumps(event), flush=True)
                else:
                    print(f"{event['timestamp']} {event['type']} {event['resource_id']}", flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        return None


def cmd_version(inv: Invocation, path: Optional[str]) -> str:
    if inv.output in ("json", "jsonl"):
        return json.dumps({"version": FAKE_VERSION, "fake": True})
    return f"dirctl version {FAKE_VERSION} (devstack fake dirctl)"


def cmd_daemon(inv: Invocation, path: Optional[str]) -> str:
    action = inv.command[1]
    if action == "stop":
        return "Daemon stopped (fake dirctl: nothing to stop)"
    where = os.environ.get(STATE_ENV) or "<unset>"
    return f"Daemon is running (fake dirctl; state file {where})"


_HANDLERS = {
    ("push",): cmd_push,
    ("pull",): cmd_pull,
    ("delete",): cmd_delete,
    ("search",): cmd_search,
    ("routing", "publish"): cmd_routing_publish,
    ("routing", "unpublish"): cmd_routing_unpublish,
    ("routing", "list"): cmd_routing_list,
    ("routing", "search"): cmd_routing_search,
    ("routing", "info"): cmd_routing_info,
    ("events", "listen"): cmd_events_listen,
}
# Commands that work without a state file.
_STATELESS = {
    ("version",): cmd_version,
    ("daemon", "start"): cmd_daemon,
    ("daemon", "stop"): cmd_daemon,
    ("daemon", "status"): cmd_daemon,
}


# ── Entry point ───────────────────────────────────────────────────────────────


def run(argv: List[str]) -> Tuple[int, str, str]:
    """Run one command; return (exit code, stdout, stderr) without printing."""
    try:
        inv = parse_args(argv)
        if inv.command in _STATELESS:
            out = _STATELESS[inv.command](inv, None)
        else:
            out = _HANDLERS[inv.command](inv, _state_path())
        return 0, (out + "\n") if out else "", ""
    except DirctlError as exc:
        if exc.code == 0:
            return 0, f"{exc}\n", ""
        return exc.code, "", f"Error: {exc}\n"


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    started = time.monotonic()
    code, out, err = run(argv)
    sys.stdout.write(out)
    sys.stderr.write(err)
    _log_invocation(argv, code, started, err)
    return code


def _log_invocation(argv: List[str], code: int, started: float, err: str) -> None:
    log_path = os.environ.get(LOG_ENV, "").strip()
    if not log_path:
        return
    entry = {
        "ts": _now_iso(),
        "pid": os.getpid(),
        "argv": argv,
        "rc": code,
        "ms": int((time.monotonic() - started) * 1000),
    }
    if err:
        entry["error"] = err.strip()[:500]
    with contextlib.suppress(OSError):
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def _route_delay() -> float:
    raw = os.environ.get(ROUTE_DELAY_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        return max(float(raw), 0.0)
    except ValueError:
        raise DirctlError(f"{ROUTE_DELAY_ENV} must be a number of seconds, got {raw!r}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    sys.exit(main())
