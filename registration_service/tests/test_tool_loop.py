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
    # Registered under the legacy name `tool_loop`; persisted as canonical `llm`
    # with tools.source='upload' (the 2026-07 consolidation).
    assert body["handler_type"] == "llm"
    assert body["config"]["tools"]["source"] == "upload"
    assert body["config"]["tools"]["entry_file"] == "tools.py"
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


# ── extra / build_tools(config) ──────────────────────────────────────────────────
# `extra` is the ONLY channel for a value a tool needs but the model must never see
# or set (an API key, a contact email, a timeout, a result cap): it is handed to
# build_tools(config), which closes over it. The wizard's tool-loop form sends it
# (static/index.html `inp-tl-extra`); these tests pin the API + engine halves.

async def test_register_tool_loop_persists_extra(client: AsyncClient) -> None:
    payload = {**TOOL_LOOP_PAYLOAD, "name": "TL Extra",
               "config": {**TOOL_LOOP_PAYLOAD["config"],
                          "extra": {"contact_email": "a@b.edu", "http_timeout": 20}}}
    resp = await client.post("/agents", json=payload)
    assert resp.status_code == 201, resp.text
    # Stored for the runtime; the API shows only the keys (values may be secrets).
    from registration_service import db
    stored = await db.get_agent(resp.json()["id"])
    assert stored["config"]["tools"]["extra"] == {
        "contact_email": "a@b.edu", "http_timeout": 20}
    assert resp.json()["config"]["tools"]["extra"] == {
        "contact_email": "[redacted]", "http_timeout": "[redacted]"}


async def test_register_tool_loop_extra_defaults_to_empty(client: AsyncClient) -> None:
    # A form that omits `extra` must still register; build_tools then sees {}.
    body = await _register(client)
    assert body["config"]["tools"]["extra"] == {}


async def test_extra_reaches_build_tools_closure(tmp_path) -> None:
    """The engine half: load_tool_module(path, extra) -> build_tools(extra), and the
    closed-over value must NOT appear as a model-visible tool parameter."""
    from agent_skeleton.spec import load_tool_module, tool_loop_spec

    src = '''
def build_tools(config):
    timeout = config.get("http_timeout", 5)
    schemas = [{
        "type": "function",
        "function": {"name": "fetch", "description": "Fetch.",
            "parameters": {"type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"], "additionalProperties": False}},
    }]
    def fetch(*, url: str) -> dict:
        return {"ok": True, "url": url, "timeout": timeout}
    return schemas, {"fetch": fetch}
'''
    path = tmp_path / "tools.py"
    path.write_text(src)

    schemas, registry = load_tool_module(str(path), {"http_timeout": 42})
    # tool_loop_spec runs the strict schema<->function alignment check.
    spec = tool_loop_spec(name="t", system_prompt="p", tool_schemas=schemas,
                          tool_registry=registry)
    assert spec.tool_registry["fetch"](url="http://x")["timeout"] == 42
    # The secret/config value is closed over, never a parameter the model can set.
    props = schemas[0]["function"]["parameters"]["properties"]
    assert set(props) == {"url"}

    # An empty config must still work — load_tool_module prefers build_tools, so a
    # factory that requires config would break every wizard registration.
    _, registry_default = load_tool_module(str(path), {})
    assert registry_default["fetch"](url="http://x")["timeout"] == 5
