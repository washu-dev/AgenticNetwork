"""Integration tests for the registration service REST API.

All tests run against the real FastAPI app with an in-memory fake DB and a
no-op FakeSupervisor (no postgres, no ADS, no subprocesses required).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from registration_service.tests.conftest import API_PROXY_PAYLOAD, LLM_PAYLOAD

pytestmark = pytest.mark.asyncio


# ── Health ─────────────────────────────────────────────────────────────────────


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ── OASF skill reference ───────────────────────────────────────────────────────


async def test_oasf_skills_returns_list(client: AsyncClient) -> None:
    r = await client.get("/oasf-skills")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) > 0
    assert "name" in body[0]


# ── Register ───────────────────────────────────────────────────────────────────


async def test_register_llm_agent(client: AsyncClient) -> None:
    r = await client.post("/agents", json=LLM_PAYLOAD)
    assert r.status_code == 201
    body = r.json()

    assert body["name"] == LLM_PAYLOAD["name"]
    assert body["handler_type"] == "llm"
    assert body["status"] == "live"
    assert body["port"] is not None
    assert body["endpoint_url"] == f"http://localhost:{body['port']}/"
    assert body["id"] is not None


async def test_register_api_proxy_agent(client: AsyncClient) -> None:
    r = await client.post("/agents", json=API_PROXY_PAYLOAD)
    assert r.status_code == 201
    body = r.json()
    # Registered under the legacy name `api_proxy`, persisted as canonical `proxy`.
    assert body["handler_type"] == "proxy"
    assert body["status"] == "live"


async def test_register_assigns_unique_ports(client: AsyncClient) -> None:
    r1 = await client.post("/agents", json=LLM_PAYLOAD)
    payload2 = {**LLM_PAYLOAD, "name": "Second Agent"}
    r2 = await client.post("/agents", json=payload2)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["port"] != r2.json()["port"]


async def test_register_missing_field_returns_422(client: AsyncClient) -> None:
    bad = {k: v for k, v in LLM_PAYLOAD.items() if k != "name"}
    r = await client.post("/agents", json=bad)
    assert r.status_code == 422


async def test_register_empty_skills_returns_422(client: AsyncClient) -> None:
    bad = {**LLM_PAYLOAD, "oasf_skills": []}
    r = await client.post("/agents", json=bad)
    assert r.status_code == 422


async def test_register_wrong_handler_type_returns_422(client: AsyncClient) -> None:
    bad = {**LLM_PAYLOAD, "handler_type": "unknown"}
    r = await client.post("/agents", json=bad)
    assert r.status_code == 422


async def test_register_no_routable_skill_returns_400(client: AsyncClient) -> None:
    """Every skill lacks an id -> the card would publish stored-but-not-routed
    (undiscoverable in ADS). Reject before spawning anything."""
    bad = {**LLM_PAYLOAD, "oasf_skills": [{"name": "custom/freeform-skill"}]}
    r = await client.post("/agents", json=bad)
    assert r.status_code == 400
    assert "routable" in r.text or "id" in r.text


async def test_register_mixed_skills_one_routable_succeeds(client: AsyncClient) -> None:
    """Only one of several skills needs an id for the card to be routable."""
    ok = {
        **LLM_PAYLOAD,
        "oasf_skills": [
            {"name": "custom/freeform-skill"},
            {"name": "nlp/text-summarization", "id": 10101},
        ],
    }
    r = await client.post("/agents", json=ok)
    assert r.status_code == 201
    assert r.json()["status"] == "live"


# ── List ───────────────────────────────────────────────────────────────────────


async def test_list_agents_empty(client: AsyncClient) -> None:
    r = await client.get("/agents")
    assert r.status_code == 200
    assert r.json() == []


async def test_list_agents_after_register(client: AsyncClient) -> None:
    await client.post("/agents", json=LLM_PAYLOAD)
    await client.post("/agents", json=API_PROXY_PAYLOAD)

    r = await client.get("/agents")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    names = {a["name"] for a in body}
    assert names == {LLM_PAYLOAD["name"], API_PROXY_PAYLOAD["name"]}


# ── Get ────────────────────────────────────────────────────────────────────────


async def test_get_agent(client: AsyncClient) -> None:
    created = (await client.post("/agents", json=LLM_PAYLOAD)).json()
    agent_id = created["id"]

    r = await client.get(f"/agents/{agent_id}")
    assert r.status_code == 200
    assert r.json()["id"] == agent_id
    assert r.json()["name"] == LLM_PAYLOAD["name"]


async def test_get_agent_not_found(client: AsyncClient) -> None:
    r = await client.get("/agents/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


async def test_get_agent_invalid_uuid_returns_422(client: AsyncClient) -> None:
    r = await client.get("/agents/not-a-uuid")
    assert r.status_code == 422


# ── Delete ─────────────────────────────────────────────────────────────────────


async def test_delete_agent(client: AsyncClient) -> None:
    agent_id = (await client.post("/agents", json=LLM_PAYLOAD)).json()["id"]

    r = await client.delete(f"/agents/{agent_id}")
    assert r.status_code == 204

    r2 = await client.get(f"/agents/{agent_id}")
    assert r2.status_code == 404


async def test_delete_frees_port(client: AsyncClient) -> None:
    """Port released on delete should be reassignable."""
    a1 = (await client.post("/agents", json=LLM_PAYLOAD)).json()
    port1 = a1["port"]

    await client.delete(f"/agents/{a1['id']}")

    a2 = (await client.post("/agents", json=LLM_PAYLOAD)).json()
    assert a2["port"] == port1  # same port reused (lowest available)


async def test_delete_agent_not_found(client: AsyncClient) -> None:
    r = await client.delete("/agents/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# ── Restart ────────────────────────────────────────────────────────────────────


async def test_restart_agent(client: AsyncClient) -> None:
    agent_id = (await client.post("/agents", json=LLM_PAYLOAD)).json()["id"]

    r = await client.post(f"/agents/{agent_id}/restart")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == agent_id
    assert body["status"] == "live"
    assert body["port"] is not None


async def test_restart_agent_not_found(client: AsyncClient) -> None:
    r = await client.post("/agents/00000000-0000-0000-0000-000000000000/restart")
    assert r.status_code == 404


# ── Skills preserved through round-trip ───────────────────────────────────────


async def test_oasf_skills_round_trip(client: AsyncClient) -> None:
    payload = {
        **LLM_PAYLOAD,
        "oasf_skills": [
            {"name": "nlp/text-summarization", "id": 10101},
            {"name": "data/analytics", "id": 20201},
        ],
    }
    created = (await client.post("/agents", json=payload)).json()
    fetched = (await client.get(f"/agents/{created['id']}")).json()

    returned_names = {s["name"] for s in fetched["oasf_skills"]}
    assert returned_names == {"nlp/text-summarization", "data/analytics"}


# ── Secret config values stay out of responses ────────────────────────────────

SECRET = "s3cret-canary"

SECRET_BEARING = [
    {
        "name": "Secret Proxy",
        "description": "a proxy with operator auth",
        "handler_type": "proxy",
        "oasf_skills": [{"name": "tool_interaction/api_schema_understanding", "id": 1401}],
        "config": {
            "upstream_url": f"https://bot:{SECRET}-pw@upstream.example/api?key={SECRET}-q&mode=fast",
            "auth_header": f"Bearer {SECRET}-header",
        },
    },
    {
        "name": "Secret Tools",
        "description": "an LLM loop over uploaded tools",
        "handler_type": "llm",
        "oasf_skills": [{"name": "nlp/text-summarization", "id": 10101}],
        "config": {
            "system_prompt": "Use your tools.",
            "tools": {"source": "upload", "extra": {"api_key": f"{SECRET}-extra"}},
        },
    },
    {
        "name": "Secret Http",
        "description": "an LLM loop over an external API",
        "handler_type": "llm",
        "oasf_skills": [{"name": "nlp/text-summarization", "id": 10101}],
        "config": {
            "tools": {
                "source": "http",
                "base_url": f"https://{SECRET}-userinfo@api.example/v1",
                "operations": [{"name": "ping", "path": "/ping"}],
            },
        },
    },
    {
        "name": "Secret Custom",
        "description": "an uploaded handler",
        "handler_type": "custom",
        "oasf_skills": [{"name": "nlp/text-summarization", "id": 10101}],
        "config": {"class_name": "H", "extra": {"token": f"{SECRET}-custom"}},
    },
]


async def test_agent_responses_never_carry_secret_config_values(client: AsyncClient) -> None:
    from registration_service import db

    ids = []
    for payload in SECRET_BEARING:
        r = await client.post("/agents", json=payload)
        assert r.status_code == 201, r.text
        assert SECRET not in r.text, payload["name"]
        ids.append(r.json()["id"])

    listed = await client.get("/agents")
    assert listed.status_code == 200 and len(listed.json()) == len(SECRET_BEARING)
    assert SECRET not in listed.text
    for agent_id in ids:
        r = await client.get(f"/agents/{agent_id}")
        assert r.status_code == 200 and SECRET not in r.text
    restarted = await client.post(f"/agents/{ids[0]}/restart")
    assert restarted.status_code == 200 and SECRET not in restarted.text

    # Keys and the non-secret shape stay visible to the wizard and approvers.
    by_name = {a["name"]: a["config"] for a in listed.json()}
    assert by_name["Secret Proxy"]["auth_header"] == "[redacted]"
    assert by_name["Secret Proxy"]["upstream_url"] == (
        "https://[redacted]@upstream.example/api?key=[redacted]&mode=[redacted]")
    assert by_name["Secret Tools"]["tools"]["extra"] == {"api_key": "[redacted]"}
    assert by_name["Secret Http"]["tools"]["base_url"] == "https://[redacted]@api.example/v1"
    assert by_name["Secret Custom"]["extra"] == {"token": "[redacted]"}

    # The stored config, which runtimes read, keeps the real values.
    for agent_id in ids:
        stored = await db.get_agent(agent_id)
        assert SECRET in str(stored["config"])


async def test_public_config_leaves_plain_configs_alone(client: AsyncClient) -> None:
    created = (await client.post("/agents", json={
        **API_PROXY_PAYLOAD, "name": "Plain Proxy",
        "config": {"upstream_url": "http://example.com/api", "auth_header": None},
    })).json()
    assert created["config"]["upstream_url"] == "http://example.com/api"
    assert created["config"]["auth_header"] is None
