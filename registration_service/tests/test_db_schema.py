"""Schema/model drift guard.

FAKE_DB=1 (used by every other test in this suite) doesn't enforce SQL CHECK
constraints, so a real-Postgres-only bug like a handler_type missing from the
`agents.handler_type` CHECK clause is invisible to the rest of the suite (it
was: 'tool_loop' was missing from both CHECK clauses in db.py, so registering a
tool_loop agent against a real Postgres would fail at INSERT). This test reads
_SCHEMA_SQL directly and cross-checks it against RegisterRequest's canonical
handler_type Literal, so future drift in either direction fails loudly without
needing a live database.
"""

from __future__ import annotations

import re
import typing

from registration_service import db
from registration_service.models import RegisterRequest

_CANONICAL_HANDLER_TYPES = set(
    typing.get_args(typing.get_type_hints(RegisterRequest)["handler_type"])
)


def _handler_type_check_clauses() -> list[set[str]]:
    """Every `handler_type IN (...)` CHECK clause in the schema SQL, as sets."""
    clauses = []
    for match in re.finditer(r"handler_type IN\s*\(([^)]*)\)", db._SCHEMA_SQL):
        values = {v.strip().strip("'") for v in match.group(1).split(",")}
        clauses.append(values)
    return clauses


def test_columns_added_later_reach_existing_databases():
    """CREATE TABLE IF NOT EXISTS skips a table that already exists, so a column
    added to it later also needs ALTER TABLE ... ADD COLUMN IF NOT EXISTS, or every
    write to it fails on a database created before the column."""
    for column in (
        "handler_file_path", "approved", "is_archive", "image_ref", "build_commit", "resources",
        "owner_email",
    ):
        assert re.search(
            rf"ALTER TABLE agents ADD COLUMN IF NOT EXISTS {column}\b", db._SCHEMA_SQL
        ), column


def test_handler_type_check_clauses_cover_all_canonical_types():
    clauses = _handler_type_check_clauses()
    # Guard the guard: fail loudly if the schema stops declaring any such
    # clause (e.g. someone rewrites the CHECK away entirely) rather than
    # silently passing on an empty list.
    assert len(clauses) == 2, (
        f"expected the inline CREATE TABLE check + the migration ADD CONSTRAINT "
        f"check, found {len(clauses)}"
    )
    for clause in clauses:
        assert clause == _CANONICAL_HANDLER_TYPES, (
            f"schema CHECK clause {clause} is out of sync with "
            f"RegisterRequest.handler_type {_CANONICAL_HANDLER_TYPES}"
        )
