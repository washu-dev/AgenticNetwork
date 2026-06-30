"""Tests for the custom-handler API surface (register / upload / approve / reject)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from registration_service.tests.conftest import (
    CUSTOM_PAYLOAD,
    INVALID_HANDLER_SRC,
    LLM_PAYLOAD,
    VALID_HANDLER_SRC,
)

pytestmark = pytest.mark.asyncio


def _upload_files(src: str = VALID_HANDLER_SRC):
    return {"file": ("handler.py", src.encode("utf-8"), "text/x-python")}


async def _register_custom(client: AsyncClient) -> dict:
    resp = await client.post("/agents", json=CUSTOM_PAYLOAD)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Registration ────────────────────────────────────────────────────────────────

async def test_register_custom_not_approved_and_not_started(client: AsyncClient) -> None:
    body = await _register_custom(client)
    assert body["handler_type"] == "custom"
    assert body["approved"] is False
    assert body["status"] == "registered"
    assert body["port"] is not None
    # Not spawned: no endpoint_url / ads_cid yet.
    assert body["endpoint_url"] is None
    assert body["ads_cid"] is None


# ── Upload ──────────────────────────────────────────────────────────────────────

async def test_upload_valid_handler(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    resp = await client.post(f"/agents/{agent['id']}/handler", files=_upload_files())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "uploaded", "errors": []}
    # File landed on disk.
    assert (tmp_path / agent["id"] / "handler.py").exists()
    # Still unapproved after upload.
    got = await client.get(f"/agents/{agent['id']}")
    assert got.json()["approved"] is False


async def test_upload_invalid_handler_returns_422(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    resp = await client.post(
        f"/agents/{agent['id']}/handler", files=_upload_files(INVALID_HANDLER_SRC)
    )
    assert resp.status_code == 422
    errors = resp.json()["detail"]["errors"]
    assert any("AgentHandler" in e for e in errors)


async def test_upload_to_non_custom_agent_404(client: AsyncClient) -> None:
    llm = (await client.post("/agents", json=LLM_PAYLOAD)).json()
    resp = await client.post(f"/agents/{llm['id']}/handler", files=_upload_files())
    assert resp.status_code == 404


# ── Approve / reject ─────────────────────────────────────────────────────────────

async def test_approve_starts_agent(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_upload_files())

    resp = await client.post(f"/agents/{agent['id']}/approve")
    assert resp.status_code == 200, resp.text

    got = (await client.get(f"/agents/{agent['id']}")).json()
    assert got["approved"] is True
    assert got["status"] == "live"  # FakeSupervisor marks it live


async def test_approve_without_handler_returns_409(client: AsyncClient) -> None:
    agent = await _register_custom(client)
    resp = await client.post(f"/agents/{agent['id']}/approve")
    assert resp.status_code == 409


async def test_reject_leaves_unapproved(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_upload_files())

    resp = await client.post(f"/agents/{agent['id']}/reject", json={"reason": "nope"})
    assert resp.status_code == 200

    got = (await client.get(f"/agents/{agent['id']}")).json()
    assert got["approved"] is False
    assert got["status"] == "registered"


# ── Handler source retrieval ─────────────────────────────────────────────────────

async def test_get_handler_source(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_upload_files())

    resp = await client.get(f"/agents/{agent['id']}/handler")
    assert resp.status_code == 200
    assert "class MyAgentHandler(AgentHandler)" in resp.json()["source"]


async def test_get_handler_source_before_upload_404(client: AsyncClient) -> None:
    agent = await _register_custom(client)
    resp = await client.get(f"/agents/{agent['id']}/handler")
    assert resp.status_code == 404


# ── Admin token enforcement ──────────────────────────────────────────────────────

async def test_admin_token_enforced(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    monkeypatch.setenv("MAIN_AGENT_ADMIN_TOKEN", "secret")
    agent = await _register_custom(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_upload_files())

    # No token → 403
    resp = await client.post(f"/agents/{agent['id']}/approve")
    assert resp.status_code == 403

    # Correct token → ok
    resp = await client.post(
        f"/agents/{agent['id']}/approve", headers={"X-Admin-Token": "secret"}
    )
    assert resp.status_code == 200


# ── Internal endpoints (host spawn loop) ─────────────────────────────────────────

async def test_internal_endpoints_exist(client: AsyncClient) -> None:
    q = await client.get("/internal/spawn-queue")
    assert q.status_code == 200 and isinstance(q.json(), list)

    s = await client.get("/internal/stop-queue")
    assert s.status_code == 200 and "pids" in s.json()

    import uuid as _uuid
    r = await client.post("/internal/spawned", json={"agent_id": str(_uuid.uuid4()), "pid": 123})
    assert r.status_code == 200


# ── Delete cleanup ───────────────────────────────────────────────────────────────

async def test_delete_custom_cleans_handler_dir(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_upload_files())
    assert (tmp_path / agent["id"]).exists()

    resp = await client.delete(f"/agents/{agent['id']}")
    assert resp.status_code == 204
    assert not (tmp_path / agent["id"]).exists()
