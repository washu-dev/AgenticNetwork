"""Tests for the `endpoint_ops` handler type — a typed per-operation API wrapper.

Registration-side wiring (Feature B): the engine capability lives in agent_skeleton
(see agent_skeleton/tests/test_operations.py); these tests cover that the new type
registers, validates, and round-trips through the REST API under FAKE_DB.
"""

from __future__ import annotations

from httpx import AsyncClient

from registration_service.models import EndpointOpsConfig, parse_config

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

def test_parse_config_builds_endpoint_ops_config():
    cfg = parse_config("endpoint_ops", ENDPOINT_OPS_PAYLOAD["config"])
    assert isinstance(cfg, EndpointOpsConfig)
    assert cfg.base_url == "https://api.example.com/v1"
    assert [op.name for op in cfg.operations] == ["get_weather", "submit_report"]
    assert cfg.operations[0].method == "GET" and cfg.operations[0].required == ["city"]


def test_operation_method_is_normalised_and_validated():
    cfg = parse_config(
        "endpoint_ops",
        {"base_url": "https://a.example", "operations": [{"name": "x", "method": "post", "path": "/x"}]},
    )
    assert cfg.operations[0].method == "POST"  # upper-cased


# ── API: register / validate / round-trip ───────────────────────────────────────

async def test_register_endpoint_ops_agent(client: AsyncClient) -> None:
    r = await client.post("/agents", json=ENDPOINT_OPS_PAYLOAD)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["handler_type"] == "endpoint_ops"
    assert body["status"] == "live"
    assert body["port"] is not None
    # Config round-trips with the typed operations intact.
    assert body["config"]["base_url"] == "https://api.example.com/v1"
    assert [op["name"] for op in body["config"]["operations"]] == ["get_weather", "submit_report"]


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
    # A config carrying an (optional) system_prompt must still parse as an
    # EndpointOpsConfig — NOT get mis-coerced to LLMConfig, which would drop
    # base_url/operations. Registering then reading it back proves both survive.
    payload = {
        **ENDPOINT_OPS_PAYLOAD,
        "name": "Weather API Agent With Prompt",
        "config": {**ENDPOINT_OPS_PAYLOAD["config"], "system_prompt": "Be terse."},
    }
    r = await client.post("/agents", json=payload)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]

    got = (await client.get(f"/agents/{agent_id}")).json()
    assert got["handler_type"] == "endpoint_ops"
    assert got["config"]["base_url"] == "https://api.example.com/v1"
    assert len(got["config"]["operations"]) == 2
    assert got["config"]["system_prompt"] == "Be terse."
