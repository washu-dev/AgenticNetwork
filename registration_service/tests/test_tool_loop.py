"""Tests for the tool_loop handler API surface (register / upload / approve).

tool_loop uploads author-supplied tool bodies + schemas + a system prompt; the
service builds the LLM loop / handler / A2A server around them. It shares the
uploaded-code lifecycle with `custom` (register-unapproved → upload → approve),
but validates the *tool contract* (TOOL_SCHEMAS/TOOL_REGISTRY or build_tools)
rather than an AgentHandler subclass.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from httpx import AsyncClient

from registration_service.tests.conftest import LLM_PAYLOAD

pytestmark = pytest.mark.asyncio


TOOL_LOOP_PAYLOAD = {
    "name": "Test Tool Loop Agent",
    "description": "An LLM tool loop assembled from uploaded tools",
    "handler_type": "tool_loop",
    "oasf_skills": [{"name": "nlp/text-summarization", "id": 10101}],
    "config": {
        "system_prompt": "You are a helpful assistant. Use your tools.",
        "model": "openai/gpt-oss-20b",
    },
}

VALID_TOOLS_SRC = '''
TOOL_SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "word_count",
        "description": "Count words.",
        "parameters": {"type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False},
    },
}]

def word_count(*, text: str) -> dict:
    return {"ok": True, "count": len(text.split())}

TOOL_REGISTRY = {"word_count": word_count}
'''

# The closure/factory form — the schema-less contract shape.
BUILD_TOOLS_SRC = '''
def build_tools(config):
    schemas = [{
        "type": "function",
        "function": {"name": "echo", "description": "Echo text.",
            "parameters": {"type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"], "additionalProperties": False}},
    }]
    def echo(*, text: str) -> dict:
        return {"ok": True, "echo": text}
    return schemas, {"echo": echo}
'''

# No tool contract at all → must be rejected.
NO_CONTRACT_SRC = "x = 1\n"


def _upload(src: str, name: str = "tools.py"):
    return {"file": (name, src.encode("utf-8"), "text/x-python")}


def _zip_upload(files: dict[str, str]):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return {"file": ("project.zip", buf.getvalue(), "application/zip")}


async def _register(client: AsyncClient) -> dict:
    resp = await client.post("/agents", json=TOOL_LOOP_PAYLOAD)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Registration ────────────────────────────────────────────────────────────────

async def test_register_tool_loop_not_approved_and_not_started(client: AsyncClient) -> None:
    body = await _register(client)
    assert body["handler_type"] == "tool_loop"
    assert body["approved"] is False
    assert body["status"] == "registered"
    assert body["has_handler"] is False
    assert body["endpoint_url"] is None and body["ads_cid"] is None


# ── Upload ──────────────────────────────────────────────────────────────────────

async def test_upload_valid_tools(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register(client)
    resp = await client.post(f"/agents/{agent['id']}/handler", files=_upload(VALID_TOOLS_SRC))
    assert resp.status_code == 200, resp.text
    assert resp.json()["errors"] == []
    assert (tmp_path / agent["id"] / "tools.py").exists()
    assert (await client.get(f"/agents/{agent['id']}")).json()["has_handler"] is True


async def test_upload_build_tools_factory_form(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register(client)
    resp = await client.post(f"/agents/{agent['id']}/handler", files=_upload(BUILD_TOOLS_SRC))
    assert resp.status_code == 200, resp.text


async def test_upload_no_contract_returns_422(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register(client)
    resp = await client.post(f"/agents/{agent['id']}/handler", files=_upload(NO_CONTRACT_SRC))
    assert resp.status_code == 422
    assert any("TOOL_SCHEMAS" in e for e in resp.json()["detail"]["errors"])


async def test_upload_to_llm_agent_404(client: AsyncClient) -> None:
    llm = (await client.post("/agents", json=LLM_PAYLOAD)).json()
    resp = await client.post(f"/agents/{llm['id']}/handler", files=_upload(VALID_TOOLS_SRC))
    assert resp.status_code == 404


async def test_upload_zip_with_custom_entry_file(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    # entry_file names the module the tools live in; a zip must carry it at the root.
    payload = {**TOOL_LOOP_PAYLOAD, "name": "TL Zip",
               "config": {**TOOL_LOOP_PAYLOAD["config"], "entry_file": "mytools.py"}}
    agent = (await client.post("/agents", json=payload)).json()
    resp = await client.post(
        f"/agents/{agent['id']}/handler",
        files=_zip_upload({"mytools.py": VALID_TOOLS_SRC, "pyproject.toml": "[project]\nname='t'\nversion='0'\n"}),
    )
    assert resp.status_code == 200, resp.text
    assert (await client.get(f"/agents/{agent['id']}")).json()["is_archive"] is True


# ── Approve / review ─────────────────────────────────────────────────────────────

async def test_approve_starts_tool_loop(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_upload(VALID_TOOLS_SRC))
    resp = await client.post(f"/agents/{agent['id']}/approve")
    assert resp.status_code == 200, resp.text
    got = (await client.get(f"/agents/{agent['id']}")).json()
    assert got["approved"] is True and got["status"] == "live"  # FakeSupervisor marks live


async def test_approve_without_upload_409(client: AsyncClient) -> None:
    agent = await _register(client)
    assert (await client.post(f"/agents/{agent['id']}/approve")).status_code == 409


async def test_inspect_shows_tool_source_no_credentials(client: AsyncClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = await _register(client)
    await client.post(f"/agents/{agent['id']}/handler", files=_upload(VALID_TOOLS_SRC))
    body = (await client.get(f"/agents/{agent['id']}/handler")).json()
    assert "TOOL_REGISTRY" in body["source"]
    assert body["credentials"] == []   # tool_loop declares no per-user creds (v1)
