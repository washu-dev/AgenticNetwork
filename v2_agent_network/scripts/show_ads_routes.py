from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from v2_agent_network.services.directory.backends import (
    _cid_from_item,
    _record_from_pull_output,
)


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
    parser = argparse.ArgumentParser(
        description="Show human-readable details for ADS records currently present in routing list."
    )
    parser.add_argument(
        "--server-addr",
        default=os.getenv("ADS_SERVER_ADDR") or os.getenv("ADS_URL") or "localhost:8888",
        help="ADS server address to query.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Local dirctl daemon data directory used to read publication timestamps.",
    )
    parser.add_argument("--output", choices=["text", "json"], default="text")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = collect_routes(server_addr=args.server_addr, data_dir=args.data_dir)
    if args.output == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0


def collect_routes(
    *,
    server_addr: str,
    data_dir: Path = DEFAULT_DATA_DIR,
    runner=subprocess.run,
) -> dict[str, Any]:
    routing_list = _run(
        ["dirctl", "--server-addr", server_addr, "routing", "list", "--output", "json"],
        runner=runner,
    )
    if not routing_list.ok:
        return {
            "server_addr": server_addr,
            "routed_count": 0,
            "routes": [],
            "error": _command_error(routing_list),
        }

    items = _routing_items(routing_list.stdout)
    publications = _publications_by_cid(data_dir / "dir.db")
    routes = [
        _route_from_item(item, server_addr=server_addr, publications=publications, runner=runner)
        for item in items
    ]
    routes.sort(key=lambda item: (item.get("name") or "", item.get("agent_id") or "", item["cid"]))
    return {
        "server_addr": server_addr,
        "routed_count": len(routes),
        "routes": routes,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"ADS server: {report['server_addr']}",
        f"Routed records: {report['routed_count']}",
    ]
    if report.get("error"):
        lines.append(f"Error: {report['error']}")
        return "\n".join(lines)
    if not report["routes"]:
        lines.append("No records in routing list.")
        return "\n".join(lines)

    for index, route in enumerate(report["routes"], start=1):
        label_count = route["label_count"]
        lines.extend(
            [
                "",
                f"{index}. {route['name'] or 'unknown'} ({route['agent_id'] or 'unknown-agent'})",
                f"   cid: {route['cid']}",
                f"   endpoint: {route['endpoint'] or 'unknown'}",
                f"   version: {route['version'] or 'unknown'}",
                f"   routed_at: {route['routed_at'] or 'unknown'}",
                f"   publish_requested_at: {route['publish_requested_at'] or 'unknown'}",
                f"   record_created_at: {route['record_created_at'] or 'unknown'}",
                f"   labels ({label_count}):",
            ]
        )
        if route["labels"]:
            lines.extend(f"     - {label}" for label in route["labels"])
        else:
            lines.append("     none")
        if route.get("pull_error"):
            lines.append(f"   pull_error: {route['pull_error']}")
    return "\n".join(lines)


def _route_from_item(
    item: Any,
    *,
    server_addr: str,
    publications: dict[str, dict[str, Any]],
    runner,
) -> dict[str, Any]:
    cid = _cid_from_item(item) or "unknown"
    labels = _labels_from_item(item)
    publication = publications.get(cid, {})
    route = {
        "cid": cid,
        "labels": labels,
        "label_count": len(labels),
        "publication_status": publication.get("status"),
        "publish_requested_at": publication.get("created_time"),
        "routed_at": publication.get("last_update_time"),
        "publication_id": publication.get("id"),
        "agent_id": None,
        "name": None,
        "endpoint": None,
        "version": None,
        "record_created_at": None,
    }
    pull = _run(["dirctl", "--server-addr", server_addr, "pull", cid, "--output", "json"], runner=runner)
    if not pull.ok:
        route["pull_error"] = _command_error(pull)
        return route
    try:
        record = _record_from_pull_output(pull.stdout)
    except json.JSONDecodeError as exc:
        route["pull_error"] = f"could not parse pull output: {exc}"
        return route

    annotations = record.get("annotations") if isinstance(record.get("annotations"), dict) else {}
    card = _embedded_agent_card(record)
    route.update(
        {
            "agent_id": annotations.get("agentic_network.agent_id") or (card or {}).get("name"),
            "name": annotations.get("agentic_network.display_name") or (card or {}).get("name") or record.get("name"),
            "endpoint": annotations.get("a2a.url") or (card or {}).get("url"),
            "version": record.get("version") or (card or {}).get("version"),
            "record_created_at": record.get("created_at"),
        }
    )
    return route


def _routing_items(raw: str) -> list[Any]:
    if not raw.strip():
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("items", "records", "results", "record_refs", "recordRefs", "refs"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    if isinstance(data, list):
        return data
    return []


def _labels_from_item(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return []
    labels = item.get("labels")
    if not isinstance(labels, list):
        return []
    return [label for label in labels if isinstance(label, str)]


def _embedded_agent_card(record: dict[str, Any]) -> dict[str, Any] | None:
    for module in record.get("modules", []):
        if not isinstance(module, dict):
            continue
        data = module.get("data")
        if isinstance(data, dict) and isinstance(data.get("card_data"), dict):
            return data["card_data"]
    return None


def _publications_by_cid(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                select id, status, created_time, last_update_time, request_json
                from publications
                order by created_time, last_update_time
                """
            ).fetchall()
    except Exception:
        return {}

    publications: dict[str, dict[str, Any]] = {}
    for row in rows:
        publication = dict(row)
        for cid in _cids_from_publication(publication):
            publications[cid] = publication
    return publications


def _cids_from_publication(publication: dict[str, Any]) -> list[str]:
    raw = publication.get("request_json") or ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    refs = data.get("recordRefs", {}).get("refs", [])
    return [item["cid"] for item in refs if isinstance(item, dict) and isinstance(item.get("cid"), str)]


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


def _command_error(result: CommandResult) -> str:
    stderr = " ".join(result.stderr.strip().split())
    stdout = " ".join(result.stdout.strip().split())
    detail = stderr or stdout or f"returncode={result.returncode}"
    return f"{' '.join(result.command)}: {detail}"


if __name__ == "__main__":
    raise SystemExit(main())
