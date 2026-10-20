"""P4a — credential declaration, catalog, and card carrier."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from registration_service.card_credentials import (
    CREDENTIALS_EXTENSION_URI,
    build_credentials_extension,
)
from agentnet.models import REQUIRED_CREDENTIALS_EXTENSION_URI
from registration_service.credential_catalog import (
    catalog_entry,
    is_known_type,
)
from registration_service.models import CustomConfig, LLMConfig
from registration_service.supervisor import _build_agent_card, _declared_credentials
from registration_service.tests.conftest import CUSTOM_PAYLOAD


# ── Catalog ──────────────────────────────────────────────────────────────────

def test_catalog_known_and_custom_types():
    assert is_known_type("openai_api_key")
    assert is_known_type("custom:my-thing")
    assert is_known_type("custom:a_b")
    assert not is_known_type("bogus")
    assert not is_known_type("custom:")          # slug required
    assert not is_known_type("custom:Bad Slug")  # no spaces/uppercase
    assert catalog_entry("openai_api_key").kind == "api_key"
    assert catalog_entry("custom:x") is None     # not a first-class catalog entry


# ── Model ────────────────────────────────────────────────────────────────────

def test_customconfig_accepts_credentials_and_round_trips():
    cfg = CustomConfig(
        class_name="H",
        credentials=[
            {"type": "openai_api_key", "label": "OpenAI key"},
            {"type": "custom:foo", "label": "Foo", "required": False, "env_var": "FOO"},
        ],
    )
    assert cfg.credentials[0].required is True          # default
    assert cfg.credentials[1].env_var == "FOO"
    # Round-trip through JSON (the AGENT_CONFIG_JSON envelope path).
    reloaded = CustomConfig.model_validate(json.loads(cfg.model_dump_json()))
    assert [c.model_dump() for c in reloaded.credentials] == [
        c.model_dump() for c in cfg.credentials
    ]


def test_customconfig_defaults_to_no_credentials():
    assert CustomConfig(class_name="H").credentials == []


def test_credential_unknown_type_rejected():
    with pytest.raises(ValidationError):
        CustomConfig(class_name="H", credentials=[{"type": "nope", "label": "x"}])


# ── Card carrier ───────────────────────────────────────────────────────────────

def _skills():
    return [{"name": "nlp/text-summarization", "key": "nlp/text-summarization", "id": 10101}]


def test_build_agent_card_emits_extension_when_declared():
    creds = [{"type": "openai_api_key", "label": "OpenAI key", "required": True, "env_var": "OPENAI_API_KEY"}]
    card = _build_agent_card("id1", "A", "desc", _skills(), 9999, creds)
    exts = card.capabilities.extensions
    assert exts and len(exts) == 1
    ext = exts[0]
    assert ext.uri == CREDENTIALS_EXTENSION_URI
    assert ext.required is False            # protocol-comprehension flag stays off (P4c enforces)
    assert ext.params["credentials"] == creds
    assert card.capabilities.streaming is True


def test_build_agent_card_omits_extension_when_empty():
    card = _build_agent_card("id1", "A", "desc", _skills(), 9999, [])
    assert card.capabilities.extensions is None
    # And the no-arg call (existing/other-type agents) is unaffected.
    assert _build_agent_card("id1", "A", "desc", _skills(), 9999).capabilities.extensions is None


def test_build_credentials_extension_helper():
    assert build_credentials_extension(None) is None
    assert build_credentials_extension([]) is None
    ext = build_credentials_extension([{"type": "brave_api_key", "label": "Brave"}])
    assert ext.uri == CREDENTIALS_EXTENSION_URI


def test_declared_credentials_only_for_custom():
    assert _declared_credentials(LLMConfig(system_prompt="hi")) == []
    cfg = CustomConfig(class_name="H", credentials=[{"type": "openai_api_key", "label": "K"}])
    assert _declared_credentials(cfg) == [{"type": "openai_api_key", "label": "K", "required": True, "env_var": None}]


# ── Approver visibility (inspect endpoint) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_inspect_endpoint_surfaces_declared_credentials(
    client: AsyncClient, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HANDLER_FILES_DIR", str(tmp_path))
    payload = json.loads(json.dumps(CUSTOM_PAYLOAD))  # deep copy
    payload["name"] = "Cred Agent"
    payload["config"]["credentials"] = [
        {"type": "openai_api_key", "label": "OpenAI key", "required": True, "env_var": "OPENAI_API_KEY"}
    ]
    agent = (await client.post("/agents", json=payload)).json()
    await client.post(
        f"/agents/{agent['id']}/handler",
        files={"file": ("handler.py",
                        b"from agent_skeleton import AgentHandler\n"
                        b"class MyAgentHandler(AgentHandler):\n"
                        b"    async def handle_structured(self, u, files=[]):\n"
                        b"        return {'answer': 'ok'}\n",
                        "text/x-python")},
    )
    body = (await client.get(f"/agents/{agent['id']}/handler")).json()
    assert body["credentials"] == [
        {"type": "openai_api_key", "label": "OpenAI key", "required": True, "env_var": "OPENAI_API_KEY"}
    ]


@pytest.mark.asyncio
async def test_credential_catalog_endpoint(client: AsyncClient) -> None:
    body = (await client.get("/credential-catalog")).json()
    types = {e["type"] for e in body}
    assert "openai_api_key" in types and "brave_api_key" in types
    assert all({"type", "label", "kind"} <= e.keys() for e in body)


def test_credentials_extension_uri_matches_agentnet() -> None:
    # card_credentials writes the URI out (it runs inside agent processes); the chat
    # app reads it through agentnet. The two copies must stay equal.
    assert CREDENTIALS_EXTENSION_URI == REQUIRED_CREDENTIALS_EXTENSION_URI
