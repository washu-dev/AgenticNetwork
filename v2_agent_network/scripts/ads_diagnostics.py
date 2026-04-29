from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from v2_agent_network.services.directory.backends import _cids_from_routing_output


DEFAULT_DATA_DIR = Path("~/.agntcy/dir").expanduser()


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose local AGNTCY ADS routing state.")
    parser.add_argument(
        "--server-addr",
        default=os.getenv("ADS_SERVER_ADDR") or os.getenv("ADS_URL") or "localhost:8888",
        help="ADS server address to query.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Local dirctl daemon data directory.",
    )
    parser.add_argument("--output", choices=["text", "json"], default="text")
    return parser.parse_args(argv)


def collect_diagnostics(
    *,
    server_addr: str,
    data_dir: Path = DEFAULT_DATA_DIR,
    runner=subprocess.run,
) -> dict[str, Any]:
    commands = {
        "dirctl_version": _run(["dirctl", "version"], runner=runner),
        "daemon_status": _run(["dirctl", "daemon", "status"], runner=runner),
        "listening_sockets": _run(["ss", "-ltnp"], runner=runner),
        "routing_info": _run(
            ["dirctl", "--server-addr", server_addr, "routing", "info", "--output", "json"],
            runner=runner,
        ),
        "routing_list": _run(
            ["dirctl", "--server-addr", server_addr, "routing", "list", "--output", "json"],
            runner=runner,
        ),
        "stored_search": _run(
            ["dirctl", "--server-addr", server_addr, "search", "--output", "json"],
            runner=runner,
        ),
    }
    db = _read_local_db(data_dir / "dir.db")
    routed_cids = set(_safe_cids(commands["routing_list"].stdout))
    stored_cids = set(_safe_cids(commands["stored_search"].stdout))
    stored_cids.update(record["record_cid"] for record in db["records"])
    publications = _publications_by_cid(db["publications"])
    classifications = [
        _classify_cid(cid, routed_cids=routed_cids, stored_cids=stored_cids, publications=publications)
        for cid in sorted(stored_cids | routed_cids)
    ]
    return {
        "server_addr": server_addr,
        "data_dir": str(data_dir),
        "commands": {name: _command_dict(result) for name, result in commands.items()},
        "routing_info": _safe_json(commands["routing_info"].stdout),
        "routed_cids": sorted(routed_cids),
        "stored_cids": sorted(stored_cids),
        "publications": db["publications"],
        "records": db["records"],
        "classifications": classifications,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"ADS server: {report['server_addr']}",
        f"Data dir: {report['data_dir']}",
        f"dirctl: {_one_line(report['commands']['dirctl_version']['stdout'])}",
        f"daemon: {_one_line(report['commands']['daemon_status']['stdout'] or report['commands']['daemon_status']['stderr'])}",
        f"routing_info: {json.dumps(report['routing_info'], sort_keys=True)}",
        "",
        "CID state:",
    ]
    if not report["classifications"]:
        lines.append("  none")
    for item in report["classifications"]:
        publication = item.get("publication") or {}
        lines.append(
            "  {cid}  {state}  publication_status={status}  updated={updated}".format(
                cid=item["cid"],
                state=item["state"],
                status=publication.get("status", "none"),
                updated=publication.get("last_update_time", "none"),
            )
        )
    lines.extend(
        [
            "",
            "Meaning:",
            "  healthy  = stored and present in routing index",
            "  queued   = routing publish was submitted but not indexed yet",
            "  orphaned = stored with no known publication row",
            "  stale    = stored but currently unrouted after prior publication/unpublication",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = collect_diagnostics(server_addr=args.server_addr, data_dir=args.data_dir)
    if args.output == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0


def _run(command: list[str], *, runner=subprocess.run) -> CommandResult:
    try:
        completed = runner(command, text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        return CommandResult(command=command, returncode=127, stdout="", stderr=str(exc))
    return CommandResult(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _read_local_db(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    if not db_path.exists():
        return {"records": [], "publications": [], "error": [f"missing {db_path}"]}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            records = [
                dict(row)
                for row in connection.execute(
                    "select record_cid, name, version, schema_version, created_at from records"
                )
            ]
            publications = [
                dict(row)
                for row in connection.execute(
                    """
                    select id, status, created_time, last_update_time, request_json
                    from publications
                    order by created_time
                    """
                )
            ]
        return {"records": records, "publications": publications}
    except Exception as exc:
        return {"records": [], "publications": [], "error": [f"{type(exc).__name__}: {exc}"]}


def _publications_by_cid(publications: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_cid: dict[str, dict[str, Any]] = {}
    for publication in publications:
        for cid in _cids_from_publication(publication):
            by_cid[cid] = publication
    return by_cid


def _cids_from_publication(publication: dict[str, Any]) -> list[str]:
    raw = publication.get("request_json") or ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    refs = data.get("recordRefs", {}).get("refs", [])
    return [item["cid"] for item in refs if isinstance(item, dict) and isinstance(item.get("cid"), str)]


def _classify_cid(
    cid: str,
    *,
    routed_cids: set[str],
    stored_cids: set[str],
    publications: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    publication = publications.get(cid)
    if cid in routed_cids:
        state = "healthy"
    elif publication and publication.get("status") == 1:
        state = "queued"
    elif publication:
        state = "stale"
    elif cid in stored_cids:
        state = "orphaned"
    else:
        state = "unknown"
    return {"cid": cid, "state": state, "publication": publication}


def _safe_cids(raw: str) -> list[str]:
    try:
        return _cids_from_routing_output(raw)
    except Exception:
        return []


def _safe_json(raw: str) -> Any:
    try:
        return json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        return raw.strip()


def _command_dict(result: CommandResult) -> dict[str, Any]:
    return {
        "command": result.command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _one_line(value: str) -> str:
    return " ".join(value.strip().split())


if __name__ == "__main__":
    raise SystemExit(main())
