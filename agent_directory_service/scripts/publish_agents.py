from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_directory_service.services.directory.card_oasf_converter import (
    agent_card_to_oasf_record,
    load_agent_card,
    validate_agent_card,
)
from agent_directory_service.services.directory.client import DirectoryClient, DirectoryUnavailable


DEFAULT_CARD_DIR = Path(__file__).parents[1] / "configs" / "agent_cards"


@dataclass(frozen=True)
class PublishRow:
    agent_id: str
    name: str
    organization: str
    skills: str
    endpoint: str
    transport: str
    record_ref: str
    backend: str


def card_paths(card_dir: Path = DEFAULT_CARD_DIR, cards: Sequence[Path] | None = None) -> list[Path]:
    if cards:
        return [Path(card) for card in cards]
    return sorted(card_dir.glob("*.card.json"))


def build_rows(paths: Sequence[Path], *, dry_run: bool) -> list[tuple[PublishRow, dict]]:
    rows = []
    for path in paths:
        card = load_agent_card(path)
        validate_agent_card(card)
        record = agent_card_to_oasf_record(card)
        row = PublishRow(
            agent_id=record["agent_id"],
            name=record["name"],
            organization=record.get("organization", "unknown"),
            skills=", ".join(skill.id for skill in card.skills),
            endpoint=record["interaction"]["endpoint"],
            transport=record["interaction"]["preferred_transport"],
            record_ref=record["record_id"],
            backend="dry-run" if dry_run else "pending",
        )
        rows.append((row, record))
    return rows


def publish_rows(rows_and_records: Sequence[tuple[PublishRow, dict]]) -> list[PublishRow]:
    directory = DirectoryClient()
    published_rows = []
    for row, record in rows_and_records:
        published = directory.publish_oasf_record(record)
        published_rows.append(
            PublishRow(
                agent_id=row.agent_id,
                name=row.name,
                organization=row.organization,
                skills=row.skills,
                endpoint=row.endpoint,
                transport=row.transport,
                record_ref=published.backend_ref or published.record_id,
                backend=published.backend,
            )
        )
    return published_rows


def render_table(rows: Sequence[PublishRow]) -> str:
    headers = [
        "agent_id",
        "name",
        "organization",
        "skills",
        "endpoint",
        "transport",
        "record_id/cid",
        "backend",
    ]
    data = [
        [
            row.agent_id,
            row.name,
            row.organization,
            row.skills,
            row.endpoint,
            row.transport,
            row.record_ref,
            row.backend,
        ]
        for row in rows
    ]
    widths = [
        max(len(str(value)) for value in [header, *(record[index] for record in data)])
        for index, header in enumerate(headers)
    ]
    lines = [
        " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "-+-".join("-" * width for width in widths),
    ]
    lines.extend(
        " | ".join(value.ljust(widths[index]) for index, value in enumerate(record))
        for record in data
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish Agent Directory Service Agent Cards to fake or real ADS.")
    parser.add_argument(
        "--card-dir",
        type=Path,
        default=DEFAULT_CARD_DIR,
        help="Directory containing *.card.json files.",
    )
    parser.add_argument(
        "--card",
        type=Path,
        action="append",
        help="Specific Agent Card file to publish. May be provided multiple times.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate and print without publishing.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    paths = card_paths(args.card_dir, args.card)
    rows_and_records = build_rows(paths, dry_run=args.dry_run)
    if args.dry_run:
        print(render_table([row for row, _ in rows_and_records]))
        return 0
    try:
        rows = publish_rows(rows_and_records)
    except DirectoryUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
