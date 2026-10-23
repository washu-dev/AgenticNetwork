"""Who may change an agent: its owner (the SSO identity that registered it) or an admin.

Delete, restart and code upload check the forwarded identity against the row's
owner_email and ADMIN_EMAILS; an agent without an owner is admin-only. FAKE_DB=1
leaves the check open for local stacks, so these tests turn that escape hatch off
for the server's access checks (the store stays in memory).
"""

from __future__ import annotations

import asyncio
import io
import re
import uuid
import zipfile
from pathlib import Path

import pytest
from httpx import AsyncClient

from registration_service import db, server
from registration_service.fake_db import FakeConnection, _store
from registration_service.tests.conftest import CUSTOM_PAYLOAD, LLM_PAYLOAD, VALID_HANDLER_SRC

pytestmark = pytest.mark.asyncio

OWNER = "owner@example.com"
STRANGER = "stranger@example.com"
ADMIN = "admin@example.com"
V2_SRC = VALID_HANDLER_SRC.replace('"ok"', '"v2"')


def _as(email: str | None) -> dict[str, str]:
    return {"mail": email} if email else {}


def _upload(src: str = VALID_HANDLER_SRC) -> dict:
    return {"file": ("handler.py", src.encode("utf-8"), "text/x-python")}


@pytest.fixture
def enforced(monkeypatch, tmp_path):
    """Production access checks: no FAKE_DB escape hatch, one admin."""
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN)
    monkeypatch.setattr(server, "fake_db_enabled", lambda: False)


async def _register(client: AsyncClient, payload: dict, email: str | None) -> str:
    resp = await client.post("/agents", json=payload, headers=_as(email))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _custom(client: AsyncClient, email: str | None) -> str:
    """A registered custom agent with its code uploaded by `email` (or an admin,
    for an agent registered without an identity)."""
    aid = await _register(client, CUSTOM_PAYLOAD, email)
    resp = await client.post(f"/agents/{aid}/handler", files=_upload(), headers=_as(email or ADMIN))
    assert resp.status_code == 200, resp.text
    return aid


# ── Recording the owner ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [CUSTOM_PAYLOAD, LLM_PAYLOAD], ids=["custom", "llm"])
async def test_registration_records_the_callers_identity_as_owner(
    client: AsyncClient, enforced, payload
) -> None:
    """Both insert paths (uploaded-code agents and the others) record it, lowercased."""
    aid = await _register(client, payload, "Owner@Example.COM ")
    assert (await db.get_agent(aid))["owner_email"] == OWNER


async def test_registration_without_an_identity_leaves_no_owner(client: AsyncClient, enforced) -> None:
    aid = await _register(client, LLM_PAYLOAD, None)
    assert (await db.get_agent(aid))["owner_email"] is None


@pytest.mark.parametrize("archive", [False, True], ids=["file", "archive"])
async def test_the_owner_is_shown_to_admins_only(client: AsyncClient, enforced, archive) -> None:
    """The admin code review names the owner (for a single file and an archive);
    the public agent list and detail do not."""
    aid = await _register(client, CUSTOM_PAYLOAD, OWNER)
    if archive:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("handler.py", VALID_HANDLER_SRC)
        files = {"file": ("project.zip", buf.getvalue(), "application/zip")}
    else:
        files = _upload()
    resp = await client.post(f"/agents/{aid}/handler", files=files, headers=_as(OWNER))
    assert resp.status_code == 200, resp.text
    review = await client.get(f"/agents/{aid}/handler", headers=_as(ADMIN))
    assert review.status_code == 200, review.text
    assert review.json()["is_archive"] is archive and review.json()["owner_email"] == OWNER
    for public in ((await client.get("/agents")).json(), (await client.get(f"/agents/{aid}")).json()):
        assert OWNER not in str(public)


# ── Delete ──────────────────────────────────────────────────────────────────────

async def test_a_stranger_cannot_delete_an_agent(client: AsyncClient, enforced, tmp_path) -> None:
    aid = await _custom(client, OWNER)
    for headers in (_as(STRANGER), {}):
        resp = await client.delete(f"/agents/{aid}", headers=headers)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "Only the agent's owner or an admin can change it"
    assert await db.get_agent(aid) is not None
    assert (Path(tmp_path) / aid).exists()


async def test_a_strangers_delete_leaves_a_live_agent_running(client: AsyncClient, enforced) -> None:
    """Refused before anything is stopped: status, port and card are unchanged."""
    aid = await _register(client, LLM_PAYLOAD, OWNER)
    before = await db.get_agent(aid)
    assert before["status"] == "live" and before["ads_cid"]
    resp = await client.delete(f"/agents/{aid}", headers=_as(STRANGER))
    assert resp.status_code == 403, resp.text
    after = await db.get_agent(aid)
    assert (after["status"], after["port"], after["ads_cid"]) == (
        before["status"], before["port"], before["ads_cid"]
    )


@pytest.mark.parametrize("caller", [OWNER, "OWNER@example.com", ADMIN])
async def test_the_owner_or_an_admin_can_delete_an_agent(
    client: AsyncClient, enforced, caller
) -> None:
    aid = await _register(client, LLM_PAYLOAD, OWNER)
    resp = await client.delete(f"/agents/{aid}", headers=_as(caller))
    assert resp.status_code == 204, resp.text
    assert await db.get_agent(aid) is None


# ── Restart ─────────────────────────────────────────────────────────────────────

async def test_a_stranger_cannot_restart_an_agent(client: AsyncClient, enforced) -> None:
    aid = await _register(client, LLM_PAYLOAD, OWNER)
    before = await db.get_agent(aid)
    resp = await client.post(f"/agents/{aid}/restart", headers=_as(STRANGER))
    assert resp.status_code == 403, resp.text
    after = await db.get_agent(aid)
    assert (after["status"], after["port"], after["updated_at"]) == (
        before["status"], before["port"], before["updated_at"]
    )


@pytest.mark.parametrize("caller", [OWNER, ADMIN])
async def test_the_owner_or_an_admin_can_restart_an_agent(
    client: AsyncClient, enforced, caller
) -> None:
    aid = await _register(client, LLM_PAYLOAD, OWNER)
    resp = await client.post(f"/agents/{aid}/restart", headers=_as(caller))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "live"


# ── Code upload ─────────────────────────────────────────────────────────────────

async def test_a_stranger_cannot_upload_code_to_an_agent(client: AsyncClient, enforced, tmp_path) -> None:
    """The stored code and the approval stay as they were: a stranger can neither
    replace the code nor take an approved agent back to review."""
    aid = await _custom(client, OWNER)
    resp = await client.post(f"/agents/{aid}/approve", headers=_as(ADMIN))
    assert resp.status_code == 200, resp.text

    resp = await client.post(f"/agents/{aid}/handler", files=_upload(V2_SRC), headers=_as(STRANGER))
    assert resp.status_code == 403, resp.text
    row = await db.get_agent(aid)
    assert row["approved"] is True and row["status"] == "live"
    assert Path(row["handler_file_path"]).read_text() == VALID_HANDLER_SRC


@pytest.mark.parametrize("caller", [OWNER, ADMIN])
async def test_the_owner_or_an_admin_can_upload_code(client: AsyncClient, enforced, caller) -> None:
    aid = await _custom(client, OWNER)
    resp = await client.post(f"/agents/{aid}/handler", files=_upload(V2_SRC), headers=_as(caller))
    assert resp.status_code == 200, resp.text
    assert Path((await db.get_agent(aid))["handler_file_path"]).read_text() == V2_SRC


# ── Agents without an owner ─────────────────────────────────────────────────────

async def test_an_agent_without_an_owner_is_admin_only(client: AsyncClient, enforced) -> None:
    """Registered without an identity, or before owners were recorded: nobody but
    an admin may change it."""
    aid = await _custom(client, None)
    no_owner = "This agent has no recorded owner, so only an admin can change it"
    for headers in (_as(STRANGER), {}):   # a caller without an identity is no owner either
        for method, url, kwargs in (
            ("POST", f"/agents/{aid}/handler", {"files": _upload(V2_SRC)}),
            ("POST", f"/agents/{aid}/restart", {}),
            ("DELETE", f"/agents/{aid}", {}),
        ):
            resp = await client.request(method, url, headers=headers, **kwargs)
            assert resp.status_code == 403, (url, headers, resp.text)
            assert resp.json()["detail"] == no_owner
    row = await db.get_agent(aid)
    assert Path(row["handler_file_path"]).read_text() == VALID_HANDLER_SRC
    assert (await client.delete(f"/agents/{aid}", headers=_as(ADMIN))).status_code == 204


async def test_a_hand_set_owner_matches_whatever_its_case(client: AsyncClient, enforced) -> None:
    """An admin hands an agent over with UPDATE; a mixed-case value still matches
    the lowercased identity."""
    aid = await _register(client, LLM_PAYLOAD, None)
    _store().agents[uuid.UUID(aid)]["owner_email"] = " New.Owner@Example.COM"
    resp = await client.post(f"/agents/{aid}/restart", headers=_as("new.owner@example.com"))
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("payload", [CUSTOM_PAYLOAD, LLM_PAYLOAD], ids=["custom", "llm"])
async def test_two_registrations_of_one_name_cannot_both_pass(
    client: AsyncClient, monkeypatch, payload
) -> None:
    """The name check and the insert run one registration at a time, on both
    registration paths, so two registrations of one name cannot both succeed."""
    # Pause just before each path's insert: the other registration runs its check
    # meanwhile unless one lock covers both the check and the insert.
    create, execute = db.create_agent, FakeConnection.execute

    async def slow_create(**kwargs):
        await asyncio.sleep(0.01)
        return await create(**kwargs)

    async def slow_execute(self, sql, *args):
        if "INSERT INTO agents" in sql:
            await asyncio.sleep(0.01)
        return await execute(self, sql, *args)

    monkeypatch.setattr(db, "create_agent", slow_create)
    monkeypatch.setattr(FakeConnection, "execute", slow_execute)
    first, second = await asyncio.gather(
        client.post("/agents", json=payload, headers=_as(OWNER)),
        client.post("/agents", json=payload, headers=_as(STRANGER)),
    )
    assert sorted([first.status_code, second.status_code]) == [201, 409]
    assert len(_store().agents) == 1


async def test_postgres_create_agent_passes_one_argument_per_placeholder(monkeypatch) -> None:
    """The fake store does not run db.create_agent's SQL, so check its INSERT on a
    stand-in connection: it names owner_email and has an argument per placeholder."""
    executed: list[tuple[str, tuple]] = []

    class _Ctx:
        def __init__(self, value=None):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *exc):
            return False

    class _Conn:
        def transaction(self):
            return _Ctx()

        async def execute(self, sql, *args):
            executed.append((sql, args))

        async def fetchrow(self, sql, *args):
            return {"id": args[0]}

    class _Pool:
        def acquire(self):
            return _Ctx(_Conn())

    async def get_pool():
        return _Pool()

    async def assign_port(conn, agent_id):
        return 19250

    monkeypatch.setattr(db, "fake_db_enabled", lambda: False)
    monkeypatch.setattr(db, "get_pool", get_pool)
    monkeypatch.setattr("registration_service.port_pool.assign_port", assign_port)
    await db.create_agent(
        name="N", description="d", handler_type="custom", oasf_skills=[], config={},
        approved=False, resources=None, owner_email=OWNER,
    )
    insert_sql, insert_args = executed[0]
    # owner_email's value is the placeholder in its position, and that argument is the owner.
    columns = [c.strip() for c in insert_sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    values = [v.strip() for v in insert_sql.split("VALUES", 1)[1].strip().strip("()").split(",")]
    placeholder = values[columns.index("owner_email")]
    assert insert_args[int(placeholder.lstrip("$")) - 1] == OWNER
    for sql, args in executed:
        assert max(int(n) for n in re.findall(r"\$(\d+)", sql)) == len(args), sql


async def test_the_checks_are_open_under_fake_db(client: AsyncClient, monkeypatch, tmp_path) -> None:
    """The local-stack escape hatch, as for the admin checks."""
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    aid = await _custom(client, OWNER)
    resp = await client.post(f"/agents/{aid}/handler", files=_upload(V2_SRC), headers=_as(STRANGER))
    assert resp.status_code == 200, resp.text
    assert (await client.delete(f"/agents/{aid}")).status_code == 204
