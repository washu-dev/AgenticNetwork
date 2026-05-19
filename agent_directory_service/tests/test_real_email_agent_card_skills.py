from __future__ import annotations

from pathlib import Path

from agent_directory_service.services.directory.card_oasf_converter import (
    agent_card_to_oasf_record,
    load_agent_card,
    oasf_record_to_discovered_agent,
    validate_agent_card,
)


EMAIL_CARD = Path("agent_directory_service/configs/agent_cards/email_agent.card.json")
READ_ONLY_SKILLS = {
    "email/search",
    "email/read",
    "email/draft",
    "email/summarize_thread",
    "email/google/status",
    "email/current_time",
    "email/calendar/status",
    "email/calendar/list_events",
    "email/docs/list",
    "email/docs/read",
    "email/sheets/list",
    "email/sheets/read",
}
WRITE_SKILLS = {
    "email/send",
    "email/calendar/create_event",
    "email/docs/create",
    "email/docs/append",
    "email/docs/replace_text",
    "email/docs/create_and_email",
    "email/sheets/create",
    "email/sheets/write",
    "email/sheets/append",
}


def test_live_email_agent_card_validates_and_exposes_expected_skills() -> None:
    card = load_agent_card(EMAIL_CARD)
    validate_agent_card(card)

    skill_ids = {skill.id for skill in card.skills}
    assert card.name == "Email Agent"
    assert card.provider.organization == "CSPL Email Agent"
    assert card.url == "http://10.229.117.223:9103/"
    assert READ_ONLY_SKILLS <= skill_ids
    assert WRITE_SKILLS <= skill_ids


def test_live_email_oasf_conversion_preserves_all_custom_skill_tags() -> None:
    card = load_agent_card(EMAIL_CARD)
    record = agent_card_to_oasf_record(card)
    custom_tags = set(record["discovery"]["custom_skill_tags"])
    skill_policies = record["policy"]["skill_policies"]

    assert record["discovery"]["official_oasf_skills"] == []
    assert READ_ONLY_SKILLS <= custom_tags
    assert WRITE_SKILLS <= custom_tags
    for skill in WRITE_SKILLS:
        assert skill_policies[skill]["action_type"] == "write"
        assert skill_policies[skill]["confirmation_required"] is True
    for skill in READ_ONLY_SKILLS:
        assert skill_policies[skill]["action_type"] == "read"
        assert skill_policies[skill]["confirmation_required"] is False

    discovered = oasf_record_to_discovered_agent(record)
    assert discovered.endpoint == "http://10.229.117.223:9103/"
    assert discovered.organization == "CSPL Email Agent"
