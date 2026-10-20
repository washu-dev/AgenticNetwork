"""The consolidated handler-type taxonomy: ``llm`` / ``proxy`` / ``custom``.

The other test modules mostly drive the LEGACY names (``api_proxy``, ``tool_loop``,
``endpoint_ops``) because that is what they were written against, and keeping them
passing is the backward-compatibility proof. This module covers the other half:

  * the CANONICAL surface — ``llm`` with each ``tools.source``, plus ``proxy``;
  * that legacy names/shapes normalize to exactly the same stored config;
  * that "does this agent ship uploaded code?" is answered by the CONFIG
    (``carries_upload``/``upload_spec``) rather than by a handler_type name, which
    is what makes the same approval gate and the same Dockerfile serve both
    uploaded-code shapes.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from registration_service.builder import generate_dockerfile
from registration_service.models import (
    CustomConfig,
    HttpTools,
    LLMConfig,
    NoTools,
    PresetTools,
    ProxyConfig,
    RawTools,
    UploadedTools,
    carries_upload,
    normalize_handler_type,
    parse_config,
    upload_spec,
)

pytestmark = pytest.mark.asyncio


# ── Unit: names ─────────────────────────────────────────────────────────────────

async def test_canonical_names_are_unchanged_by_normalization() -> None:
    for t in ("llm", "proxy", "custom"):
        assert normalize_handler_type(t) == t


async def test_legacy_names_normalize() -> None:
    assert normalize_handler_type("api_proxy") == "proxy"
    assert normalize_handler_type("tool_loop") == "llm"
    assert normalize_handler_type("endpoint_ops") == "llm"


async def test_unknown_name_passes_through_for_the_callers_error_path() -> None:
    assert normalize_handler_type("nonsense") == "nonsense"
    with pytest.raises(ValueError, match="Unknown handler_type"):
        parse_config("nonsense", {})


# ── Unit: tool sources ──────────────────────────────────────────────────────────

async def test_llm_defaults_to_no_tools() -> None:
    cfg = parse_config("llm", {"system_prompt": "hi"})
    assert isinstance(cfg, LLMConfig) and isinstance(cfg.tools, NoTools)
    assert not carries_upload(cfg)


async def test_llm_with_preset_tools_canonical_and_legacy_agree() -> None:
    canonical = parse_config(
        "llm", {"system_prompt": "hi", "tools": {"source": "presets", "names": ["calculator"]}}
    )
    legacy = parse_config("llm", {"system_prompt": "hi", "preset_tools": ["calculator"]})
    assert isinstance(canonical.tools, PresetTools) and canonical.tools.names == ["calculator"]
    assert canonical.model_dump() == legacy.model_dump()


async def test_legacy_empty_preset_and_tools_lists_become_no_tools() -> None:
    # Configs persisted by the OLD code always carry both keys (model_dump), usually
    # empty. They must land on NoTools, not fail the discriminated union.
    cfg = parse_config("llm", {"system_prompt": "hi", "preset_tools": [], "tools": []})
    assert isinstance(cfg.tools, NoTools)


async def test_legacy_raw_tool_schemas_land_on_raw_source() -> None:
    # The old `tools: list[dict]` field was accepted but never wired. It now parses
    # into an explicit source instead of being silently dropped.
    schema = {"type": "function", "function": {"name": "x", "parameters": {}}}
    cfg = parse_config("llm", {"system_prompt": "hi", "tools": [schema]})
    assert isinstance(cfg.tools, RawTools) and cfg.tools.schemas == [schema]
    assert not carries_upload(cfg)


async def test_system_prompt_required_except_for_http_tools() -> None:
    with pytest.raises(ValueError, match="system_prompt is required"):
        parse_config("llm", {"tools": {"source": "presets", "names": []}})
    # http generates a routing prompt from the operations, so none is needed.
    cfg = parse_config("llm", {"tools": {
        "source": "http", "base_url": "https://a.example",
        "operations": [{"name": "x", "path": "/x"}],
    }})
    assert isinstance(cfg.tools, HttpTools) and cfg.system_prompt is None


# ── Unit: the uploaded-code predicate ───────────────────────────────────────────

async def test_upload_spec_covers_both_uploaded_code_shapes() -> None:
    custom = parse_config("custom", {"class_name": "H", "requirements": ["httpx"]})
    llm_upload = parse_config("llm", {
        "system_prompt": "hi",
        "tools": {"source": "upload", "entry_file": "mytools.py", "requirements": ["httpx"]},
    })
    assert carries_upload(custom) and carries_upload(llm_upload)
    assert upload_spec(custom).entry_file == "handler.py"
    assert upload_spec(llm_upload).entry_file == "mytools.py"
    assert upload_spec(custom).requirements == upload_spec(llm_upload).requirements


async def test_non_uploading_configs_have_no_upload_spec() -> None:
    for cfg in (
        parse_config("llm", {"system_prompt": "hi"}),
        parse_config("proxy", {"upstream_url": "http://x.example"}),
        parse_config("llm", {"tools": {"source": "http", "base_url": "https://a.example",
                                       "operations": [{"name": "x", "path": "/x"}]}}),
    ):
        assert upload_spec(cfg) is None
        assert not carries_upload(cfg)


async def test_dockerfile_is_identical_for_both_uploaded_shapes() -> None:
    """One builder, no handler_type branch: the only difference should be the entry file."""
    custom = CustomConfig(class_name="H", entry_file="handler.py", requirements=["httpx"],
                          system_packages=["build-essential"])
    llm_upload = LLMConfig(system_prompt="hi", tools=UploadedTools(
        entry_file="handler.py", requirements=["httpx"], system_packages=["build-essential"]))
    assert generate_dockerfile(custom) == generate_dockerfile(llm_upload)


async def test_dockerfile_rejects_a_config_with_no_upload() -> None:
    with pytest.raises(ValueError, match="requires an uploaded-code config"):
        generate_dockerfile(ProxyConfig(upstream_url="http://x.example"))


# ── API: the canonical surface ──────────────────────────────────────────────────

CANONICAL_PROXY = {
    "name": "Canonical Proxy",
    "description": "relays to an upstream I host",
    "handler_type": "proxy",
    "oasf_skills": [{"name": "tool_interaction/api_schema_understanding", "id": 1401}],
    "config": {"upstream_url": "http://example.com/api"},
}

CANONICAL_LLM_PRESETS = {
    "name": "Canonical LLM Presets",
    "description": "a prompt plus catalog tools",
    "handler_type": "llm",
    "oasf_skills": [{"name": "nlp/text-summarization", "id": 10101}],
    "config": {
        "system_prompt": "You are helpful.",
        "tools": {"source": "presets", "names": ["calculator"]},
    },
}

CANONICAL_LLM_UPLOAD = {
    "name": "Canonical LLM Upload",
    "description": "an LLM loop over uploaded tool bodies",
    "handler_type": "llm",
    "oasf_skills": [{"name": "nlp/text-summarization", "id": 10101}],
    "config": {
        "system_prompt": "You are helpful. Use your tools.",
        "tools": {"source": "upload", "extra": {"http_timeout": 20}},
    },
}

TOOLS_SRC = '''
TOOL_SCHEMAS = [{
    "type": "function",
    "function": {"name": "word_count", "description": "Count words.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"], "additionalProperties": False}},
}]

def word_count(*, text: str) -> dict:
    return {"ok": True, "count": len(text.split())}

TOOL_REGISTRY = {"word_count": word_count}
'''


async def test_register_canonical_proxy(client: AsyncClient) -> None:
    r = await client.post("/agents", json=CANONICAL_PROXY)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["handler_type"] == "proxy"
    assert body["status"] == "live" and body["port"] is not None
    assert body["approved"] is True          # no uploaded code → no approval gate


async def test_register_canonical_llm_with_presets(client: AsyncClient) -> None:
    r = await client.post("/agents", json=CANONICAL_LLM_PRESETS)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["handler_type"] == "llm"
    assert body["config"]["tools"] == {"source": "presets", "names": ["calculator"]}
    assert body["status"] == "live" and body["approved"] is True


async def test_canonical_llm_upload_is_approval_gated_end_to_end(
    client: AsyncClient, monkeypatch, tmp_path
) -> None:
    """An `llm` agent that ships code must walk the SAME lifecycle as `custom`:
    registered-unapproved → upload → approve → live. This is the whole point of
    moving the gate off the handler_type name."""
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))

    r = await client.post("/agents", json=CANONICAL_LLM_UPLOAD)
    assert r.status_code == 201, r.text
    agent = r.json()
    assert agent["handler_type"] == "llm"
    assert agent["approved"] is False and agent["status"] == "registered"
    # Not started: no endpoint published, nothing in ADS. (A port IS reserved at
    # registration; only publishing waits for approval.)
    assert agent["endpoint_url"] is None and agent["ads_cid"] is None
    assert agent["config"]["tools"]["extra"] == {"http_timeout": "[redacted]"}

    # Approving before the code arrives is a conflict, exactly as for `custom`.
    assert (await client.post(f"/agents/{agent['id']}/approve")).status_code == 409

    up = await client.post(
        f"/agents/{agent['id']}/handler",
        files={"file": ("tools.py", TOOLS_SRC.encode(), "text/x-python")},
    )
    assert up.status_code == 200, up.text
    assert up.json()["errors"] == []
    assert (tmp_path / agent["id"] / "tools.py").exists()

    assert (await client.post(f"/agents/{agent['id']}/approve")).status_code == 200
    got = (await client.get(f"/agents/{agent['id']}")).json()
    assert got["approved"] is True and got["status"] == "live"


async def test_canonical_llm_upload_validates_the_tool_contract_not_a_handler(
    client: AsyncClient, monkeypatch, tmp_path
) -> None:
    # Which validator runs is decided by the config, so an `llm` upload gets the
    # TOOL_SCHEMAS/TOOL_REGISTRY check even though its type is no longer 'tool_loop'.
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    agent = (await client.post("/agents", json=CANONICAL_LLM_UPLOAD)).json()
    r = await client.post(
        f"/agents/{agent['id']}/handler",
        files={"file": ("tools.py", b"x = 1\n", "text/x-python")},
    )
    assert r.status_code == 422
    assert any("TOOL_SCHEMAS" in e for e in r.json()["detail"]["errors"])


async def test_prompt_only_llm_cannot_be_uploaded_to(client: AsyncClient) -> None:
    # No uploaded code → the handler endpoints must 404, not accept a stray file.
    agent = (await client.post("/agents", json=CANONICAL_LLM_PRESETS)).json()
    r = await client.post(
        f"/agents/{agent['id']}/handler",
        files={"file": ("tools.py", TOOLS_SRC.encode(), "text/x-python")},
    )
    assert r.status_code == 404


# ── API: legacy names produce the same stored config ────────────────────────────

async def test_legacy_and_canonical_registrations_store_the_same_config(
    client: AsyncClient,
) -> None:
    legacy = await client.post("/agents", json={
        "name": "Legacy Proxy Form", "description": "d", "handler_type": "api_proxy",
        "oasf_skills": [{"name": "tool_interaction/api_schema_understanding", "id": 1401}],
        "config": {"upstream_url": "http://example.com/api"},
    })
    canonical = await client.post("/agents", json={
        **CANONICAL_PROXY, "name": "Canonical Proxy Form",
    })
    assert legacy.status_code == 201 and canonical.status_code == 201
    assert legacy.json()["handler_type"] == canonical.json()["handler_type"] == "proxy"
    assert legacy.json()["config"] == canonical.json()["config"]


async def test_stored_legacy_row_still_deserializes(client: AsyncClient) -> None:
    """A row written before the consolidation keeps its old handler_type AND its old
    config JSON; the read path must normalize it rather than choke. This is what the
    widened db.py CHECK constraint exists to allow."""
    from registration_service.supervisor import _deserialize_config

    old_tool_loop_row = {
        "handler_type": "tool_loop",
        "config": {"system_prompt": "hi", "model": "m", "entry_file": "tools.py",
                   "requirements": ["httpx"], "install_project": True,
                   "extra": {"k": "v"}, "python_version": "3.11",
                   "system_packages": []},
    }
    cfg = _deserialize_config(old_tool_loop_row)
    assert isinstance(cfg, LLMConfig) and isinstance(cfg.tools, UploadedTools)
    assert carries_upload(cfg)
    spec = upload_spec(cfg)
    assert spec.entry_file == "tools.py" and spec.extra == {"k": "v"}

    old_endpoint_ops_row = {
        "handler_type": "endpoint_ops",
        "config": {"base_url": "https://a.example", "auth_env": "K",
                   "operations": [{"name": "x", "path": "/x"}]},
    }
    cfg = _deserialize_config(old_endpoint_ops_row)
    assert isinstance(cfg.tools, HttpTools) and cfg.tools.auth_env == "K"

    old_api_proxy_row = {
        "handler_type": "api_proxy",
        "config": {"upstream_url": "http://x.example", "auth_header": None},
    }
    assert isinstance(_deserialize_config(old_api_proxy_row), ProxyConfig)
