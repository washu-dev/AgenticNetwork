"""Tests for the custom-handler API surface (register / upload / approve / reject)."""

from __future__ import annotations

import io
import zipfile

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


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _zip_upload(files: dict[str, str]):
    return {"file": ("project.zip", _zip_bytes(files), "application/zip")}


# A valid multi-file project: the entry adapter imports a sibling module.
_ZIP_TREE = {
    "handler.py": (
        "from agent_skeleton import AgentHandler\n"
        "from mylib import tag\n"
        "class MyAgentHandler(AgentHandler):\n"
        "    async def handle_structured(self, u, files=[]):\n"
        "        return {'answer': tag(u)}\n"
    ),
    "mylib/__init__.py": "def tag(s): return 'tagged:' + s\n",
    "pyproject.toml": "[project]\nname = 'demo'\nversion = '0.1'\n",
}


async def _register_custom(client: AsyncClient) -> dict:
    resp = await client.post("/agents", json=CUSTOM_PAYLOAD)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Registration ────────────────────────────────────────────────────────────────

async def test_duplicate_name_slug_rejected(client: AsyncClient) -> None:
    await _register_custom(client)  # "Test Custom Agent" → slug test-custom-agent
    # Same slug via different casing → 409 ("test custom agent" → test-custom-agent)
    dup = {**CUSTOM_PAYLOAD, "name": "test custom agent"}
    assert (await client.post("/agents", json=dup)).status_code == 409
    # A genuinely distinct name is allowed
    ok = {**CUSTOM_PAYLOAD, "name": "Test Custom Agent 2"}
    assert (await client.post("/agents", json=ok)).status_code == 201


async def test_register_custom_not_approved_and_not_started(client: AsyncClient) -> None:
    body = await _register_custom(client)
    assert body["handler_type"] == "custom"
    assert body["approved"] is False
    assert body["status"] == "registered"
    assert body["port"] is not None
    assert body["has_handler"] is False   # no handler uploaded yet
    # Not spawned: no endpoint_url / ads_cid yet.
    assert body["endpoint_url"] is None
    assert body["ads_cid"] is None


async def test_has_handler_flips_true_after_upload(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    assert (await client.get(f"/agents/{agent['id']}")).json()["has_handler"] is False
    await client.post(f"/agents/{agent['id']}/handler", files=_upload_files())
    assert (await client.get(f"/agents/{agent['id']}")).json()["has_handler"] is True


# ── Upload ──────────────────────────────────────────────────────────────────────

async def test_upload_valid_handler(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    resp = await client.post(f"/agents/{agent['id']}/handler", files=_upload_files())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "uploaded", "errors": [], "warnings": []}
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


async def test_blocklist_is_advisory_not_blocking(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    src = (
        "import subprocess\n"
        "from agent_skeleton import AgentHandler\n"
        "class MyAgentHandler(AgentHandler):\n"
        "    async def handle_structured(self, u, files=[]):\n"
        "        return {'answer': 'ok'}\n"
    )
    resp = await client.post(f"/agents/{agent['id']}/handler", files=_upload_files(src))
    assert resp.status_code == 200, resp.text  # advisory, not a block (docs/DECISIONS.md D2)
    assert any("subprocess" in w for w in resp.json()["warnings"])


# ── Zip (multi-file) upload ──────────────────────────────────────────────────────

async def test_upload_zip_stores_tree(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    resp = await client.post(f"/agents/{agent['id']}/handler", files=_zip_upload(_ZIP_TREE))
    assert resp.status_code == 200, resp.text
    # Whole tree stored, structure preserved.
    base = tmp_path / agent["id"]
    assert (base / "handler.py").exists()
    assert (base / "mylib" / "__init__.py").exists()
    assert (base / "pyproject.toml").exists()
    # Row flagged as an archive; still unapproved.
    got = (await client.get(f"/agents/{agent['id']}")).json()
    assert got["is_archive"] is True
    assert got["approved"] is False
    assert got["has_handler"] is True


async def test_upload_zip_missing_entry_422(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)  # entry_file defaults to handler.py
    resp = await client.post(f"/agents/{agent['id']}/handler", files=_zip_upload({"other.py": "x = 1"}))
    assert resp.status_code == 422
    assert any("handler.py" in e for e in resp.json()["detail"]["errors"])


async def test_upload_zip_zipslip_422(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    files = {**_ZIP_TREE, "../evil.py": "boom"}
    resp = await client.post(f"/agents/{agent['id']}/handler", files=_zip_upload(files))
    assert resp.status_code == 422
    assert any("escapes" in e for e in resp.json()["detail"]["errors"])


@pytest.mark.parametrize("entry_file", [
    "../escape.py", "/tmp/abs.py", "sub/handler.py", "..\\escape.py", "handler.py\nRUN id", "handler",
])
async def test_entry_file_must_be_bare_name(client: AsyncClient, entry_file: str) -> None:
    # The single-file upload writes {handler_dir}/{entry_file}, so a path would escape it.
    custom = {**CUSTOM_PAYLOAD, "config": {**CUSTOM_PAYLOAD["config"], "entry_file": entry_file}}
    assert (await client.post("/agents", json=custom)).status_code == 422
    tools = {"source": "upload", "entry_file": entry_file}
    llm = {**LLM_PAYLOAD, "config": {**LLM_PAYLOAD["config"], "tools": tools}}
    assert (await client.post("/agents", json=llm)).status_code == 422


@pytest.mark.parametrize("entry_file", ["agentnet.py", "agent_skeleton.py", "registration_service.py"])
async def test_entry_file_must_not_take_a_reserved_package_name(client: AsyncClient, entry_file: str) -> None:
    # A single-file upload is stored under this name in the project folder, which a
    # locally run agent has first on its import path, so it would replace that package.
    custom = {**CUSTOM_PAYLOAD, "config": {**CUSTOM_PAYLOAD["config"], "entry_file": entry_file}}
    resp = await client.post("/agents", json=custom)
    assert resp.status_code == 422 and "reserved platform package name" in resp.text, resp.text
    tools = {"source": "upload", "entry_file": entry_file}
    llm = {**LLM_PAYLOAD, "config": {**LLM_PAYLOAD["config"], "tools": tools}}
    assert (await client.post("/agents", json=llm)).status_code == 422


async def test_upload_zip_bad_structural_entry_422(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    # Entry present but doesn't subclass AgentHandler → structural failure blocks.
    files = {"handler.py": "class MyAgentHandler:\n    pass\n"}
    resp = await client.post(f"/agents/{agent['id']}/handler", files=_zip_upload(files))
    assert resp.status_code == 422
    assert any("AgentHandler" in e for e in resp.json()["detail"]["errors"])
    # Rejected tree is not left on disk.
    base = tmp_path / agent["id"]
    assert not base.exists() or not any(base.iterdir())


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


async def test_inspect_single_file_returns_source(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_upload_files())
    body = (await client.get(f"/agents/{agent['id']}/handler")).json()
    assert body["is_archive"] is False
    assert "class MyAgentHandler(AgentHandler)" in body["source"]
    assert body["warnings"] == []


async def test_inspect_multifile_returns_manifest(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_zip_upload(_ZIP_TREE))
    body = (await client.get(f"/agents/{agent['id']}/handler")).json()
    assert body["is_archive"] is True
    assert body["entry_file"] == "handler.py"
    assert body["manifest"] == ["handler.py", "mylib/__init__.py", "pyproject.toml"]
    assert body["download_url"].endswith(f"/agents/{agent['id']}/handler/download")
    assert "source" not in body


async def test_download_multifile_zip(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_zip_upload(_ZIP_TREE))
    resp = await client.get(f"/agents/{agent['id']}/handler/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert set(names) == {"handler.py", "mylib/__init__.py", "pyproject.toml"}


async def test_download_single_file_404(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register_custom(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_upload_files())
    # Single-file agents have no archive to download.
    resp = await client.get(f"/agents/{agent['id']}/handler/download")
    assert resp.status_code == 404


# ── Admin allowlist enforcement (P4f) ─────────────────────────────────────────────

class _FakeReq:
    """Minimal stand-in — _require_admin only reads request.headers.get(...)."""
    def __init__(self, headers: dict) -> None:
        self.headers = headers


async def test_require_admin_allowlist(monkeypatch) -> None:
    from fastapi import HTTPException
    from registration_service.server import _require_admin

    # FAKE_DB=1 is the dev/test escape hatch → admin is open (no Shibboleth locally).
    monkeypatch.setenv("FAKE_DB", "1")
    _require_admin(_FakeReq({}))  # no raise

    # Production (no FAKE_DB): enforce ADMIN_EMAILS on the forwarded SSO identity.
    monkeypatch.delenv("FAKE_DB", raising=False)
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    _require_admin(_FakeReq({"mail": "admin@example.com"}))          # allowlisted → ok
    _require_admin(_FakeReq({"mail": " ", "eppn": "Admin@Example.com"}))  # blank mail → eppn

    for headers in (
        {"mail": "bob@example.com"},                                 # non-admin
        {},                                                          # no identity
        {"mail": "bob@example.com", "eppn": "admin@example.com"},    # mail comes first
        {"remote_user": "admin@example.com"},                        # not an identity header
    ):
        with pytest.raises(HTTPException) as ei:
            _require_admin(_FakeReq(headers))
        assert ei.value.status_code == 403

    # Empty allowlist → nobody is admin (fail closed).
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    with pytest.raises(HTTPException):
        _require_admin(_FakeReq({"mail": "admin@example.com"}))


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
