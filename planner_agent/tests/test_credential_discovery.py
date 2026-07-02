"""P4b — planner reads declared credentials off the discovered card (read-only)."""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentSkill

from planner_agent.main_agent.directory.card_oasf_converter import (
    REQUIRED_CREDENTIALS_EXTENSION_URI,
    _required_credentials_from_card,
    agent_card_to_oasf_record,
    oasf_record_to_discovered_agent,
)
from planner_agent.main_agent.models import DiscoveredAgent, RiskLevel
from planner_agent.main_agent.runtime import _agent_full_card

_CREDS = [
    {"type": "openai_api_key", "label": "OpenAI key", "required": True, "env_var": "OPENAI_API_KEY"},
    {"type": "custom:weather", "label": "Weather API", "required": False, "env_var": None},
]


def _card(extensions=None) -> AgentCard:
    return AgentCard(
        name="Cred Agent",
        description="declares credentials",
        url="http://10.0.0.9:19110/",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, extensions=extensions),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        skills=[AgentSkill(id="cred/handle", name="handle", description="do it", tags=["cred/handle"])],
    )


def _cred_extension(credentials=_CREDS) -> AgentExtension:
    return AgentExtension(
        uri=REQUIRED_CREDENTIALS_EXTENSION_URI,
        description="User credentials this agent consumes",
        required=False,
        params={"credentials": credentials},
    )


# ── _required_credentials_from_card (unit) ───────────────────────────────────

def test_parses_declared_credentials():
    creds = _required_credentials_from_card(_card([_cred_extension()]))
    assert [c.model_dump() for c in creds] == _CREDS


def test_no_extensions_is_empty():
    assert _required_credentials_from_card(_card(None)) == []
    assert _required_credentials_from_card(_card([])) == []


def test_other_extension_uri_ignored():
    other = AgentExtension(uri="some.other/ext", description="x", required=False, params={"credentials": _CREDS})
    assert _required_credentials_from_card(_card([other])) == []


def test_malformed_params_do_not_crash():
    # params missing "credentials"
    e1 = AgentExtension(uri=REQUIRED_CREDENTIALS_EXTENSION_URI, description="x", required=False, params={})
    assert _required_credentials_from_card(_card([e1])) == []
    # credentials not a list
    e2 = AgentExtension(uri=REQUIRED_CREDENTIALS_EXTENSION_URI, description="x", required=False, params={"credentials": "nope"})
    assert _required_credentials_from_card(_card([e2])) == []


def test_junk_entry_skipped_valid_kept():
    mixed = [
        "not-a-dict",
        {"label": "missing type"},            # invalid: no type
        {"type": "brave_api_key", "label": "Brave"},  # valid
    ]
    creds = _required_credentials_from_card(_card([_cred_extension(mixed)]))
    assert [c.type for c in creds] == ["brave_api_key"]
    assert creds[0].required is True  # default applied


# ── Full record round-trip (proves the real normalization path) ──────────────

def test_round_trip_through_oasf_record():
    record = agent_card_to_oasf_record(_card([_cred_extension()]))
    agent = oasf_record_to_discovered_agent(record)
    assert [c.model_dump() for c in agent.required_credentials] == _CREDS


def test_round_trip_no_credentials_is_empty():
    agent = oasf_record_to_discovered_agent(agent_card_to_oasf_record(_card(None)))
    assert agent.required_credentials == []


# ── LLM surface ──────────────────────────────────────────────────────────────

def test_agent_full_card_does_not_leak_required_credentials():
    # Credentials are handled out-of-band (Settings + consent gate). Surfacing the
    # requirement to the LLM made it solicit API keys in chat, so it's excluded.
    agent = DiscoveredAgent(
        agent_id="a1", name="Cred Agent", organization="org", description="d",
        endpoint="http://10.0.0.9:19110/", source_record_id="r1",
        risk_level=RiskLevel.low,
        required_credentials=[{"type": "openai_api_key", "label": "OpenAI key"}],
    )
    assert "required_credentials" not in _agent_full_card(agent)
