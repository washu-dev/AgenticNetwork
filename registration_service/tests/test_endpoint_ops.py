"""Tests for typed per-operation API wrapping — the LEGACY `endpoint_ops` handler type.

After the 2026-07 consolidation this is `llm` with `tools.source == "http"`
(models.HttpTools). The legacy `endpoint_ops` name and its flat config shape are
still accepted on the wire and still parse, because rows written before the change
carry them; these tests pin that backward compatibility, and assert the agent is
now *persisted* under the canonical type. The engine capability itself lives in
agent_skeleton (see agent_skeleton/tests/test_operations.py).
"""

from __future__ import annotations

from httpx import AsyncClient

from registration_service.models import HttpTools, LLMConfig, parse_config

ENDPOINT_OPS_PAYLOAD = {
    "name": "Weather API Agent",
    "description": "wraps the weather API as typed operations",
    "handler_type": "endpoint_ops",
    "oasf_skills": [{"name": "tool_interaction/api_schema_understanding", "id": 1401}],
    "config": {
        "base_url": "https://api.example.com/v1",
        "auth_env": "WEATHER_API_KEY",
        "operations": [
            {
                "name": "get_weather",
                "method": "GET",
                "path": "/weather",
                "description": "current weather for a city",
                "params": {"city": {"type": "string"}, "units": {"type": "string"}},
                "required": ["city"],
            },
            {
                "name": "submit_report",
                "method": "POST",
                "path": "/reports",
                "params": {"location": {"type": "string"}},
                "required": ["location"],
            },
        ],
    },
}


# ── Unit: parse_config discriminates by handler_type ────────────────────────────

def test_parse_config_builds_llm_config_with_http_tools():
    """The legacy flat shape becomes LLMConfig(tools=HttpTools(...))."""
    cfg = parse_config("endpoint_ops", ENDPOINT_OPS_PAYLOAD["config"])
    assert isinstance(cfg, LLMConfig)
    assert isinstance(cfg.tools, HttpTools)
    assert cfg.tools.base_url == "https://api.example.com/v1"
    assert cfg.tools.auth_env == "WEATHER_API_KEY"
    assert [op.name for op in cfg.tools.operations] == ["get_weather", "submit_report"]
    assert cfg.tools.operations[0].method == "GET"
    assert cfg.tools.operations[0].required == ["city"]


def test_canonical_http_tools_shape_parses_directly():
    """The same agent expressed in the canonical shape (no legacy name involved)."""
    cfg = parse_config("llm", {
        "tools": {**ENDPOINT_OPS_PAYLOAD["config"], "source": "http"},
    })
    assert isinstance(cfg, LLMConfig) and isinstance(cfg.tools, HttpTools)
    assert [op.name for op in cfg.tools.operations] == ["get_weather", "submit_report"]
    # system_prompt stays optional for http tools: a routing prompt is generated.
    assert cfg.system_prompt is None


def test_http_tools_do_not_require_a_system_prompt():
    # Every other tool source does require one.
    import pytest as _pytest
    with _pytest.raises(ValueError):
        parse_config("llm", {"tools": {"source": "presets", "names": ["calculator"]}})


def test_operation_method_is_normalised_and_validated():
    cfg = parse_config(
        "endpoint_ops",
        {"base_url": "https://a.example", "operations": [{"name": "x", "method": "post", "path": "/x"}]},
    )
    assert cfg.tools.operations[0].method == "POST"  # upper-cased


# ── API: register / validate / round-trip ───────────────────────────────────────

async def test_register_endpoint_ops_agent(client: AsyncClient) -> None:
    r = await client.post("/agents", json=ENDPOINT_OPS_PAYLOAD)
    assert r.status_code == 201, r.text
    body = r.json()
    # Registered under the LEGACY name, persisted under the CANONICAL one.
    assert body["handler_type"] == "llm"
    assert body["status"] == "live"
    assert body["port"] is not None
    # Config round-trips with the typed operations intact, now under config.tools.
    assert body["config"]["tools"]["source"] == "http"
    assert body["config"]["tools"]["base_url"] == "https://api.example.com/v1"
    assert [op["name"] for op in body["config"]["tools"]["operations"]] == [
        "get_weather", "submit_report"]


async def test_endpoint_ops_missing_base_url_returns_422(client: AsyncClient) -> None:
    bad = {**ENDPOINT_OPS_PAYLOAD, "config": {"operations": [{"name": "x", "path": "/x"}]}}
    r = await client.post("/agents", json=bad)
    assert r.status_code == 422


async def test_endpoint_ops_empty_operations_returns_422(client: AsyncClient) -> None:
    bad = {**ENDPOINT_OPS_PAYLOAD, "config": {"base_url": "https://a.example", "operations": []}}
    r = await client.post("/agents", json=bad)
    assert r.status_code == 422


async def test_endpoint_ops_bad_method_returns_422(client: AsyncClient) -> None:
    bad = {
        **ENDPOINT_OPS_PAYLOAD,
        "config": {
            "base_url": "https://a.example",
            "operations": [{"name": "x", "method": "TELEPORT", "path": "/x"}],
        },
    }
    r = await client.post("/agents", json=bad)
    assert r.status_code == 422


async def test_endpoint_ops_non_http_base_url_returns_422(client: AsyncClient) -> None:
    bad = {
        **ENDPOINT_OPS_PAYLOAD,
        "config": {"base_url": "ftp://a.example", "operations": [{"name": "x", "path": "/x"}]},
    }
    r = await client.post("/agents", json=bad)
    assert r.status_code == 422


async def test_endpoint_ops_with_system_prompt_is_not_miscoerced(client: AsyncClient) -> None:
    # A legacy config carrying an (optional) system_prompt must keep its base_url +
    # operations. Before the consolidation the risk was structural mis-coercion to
    # LLMConfig; now the risk is the legacy->HttpTools conversion dropping fields.
    # Registering then reading it back proves everything survives.
    payload = {
        **ENDPOINT_OPS_PAYLOAD,
        "name": "Weather API Agent With Prompt",
        "config": {**ENDPOINT_OPS_PAYLOAD["config"], "system_prompt": "Be terse."},
    }
    r = await client.post("/agents", json=payload)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]

    got = (await client.get(f"/agents/{agent_id}")).json()
    assert got["handler_type"] == "llm"
    assert got["config"]["tools"]["base_url"] == "https://api.example.com/v1"
    assert len(got["config"]["tools"]["operations"]) == 2
    assert got["config"]["system_prompt"] == "Be terse."
