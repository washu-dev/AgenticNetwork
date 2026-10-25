"""agentnet.settings: DATABASE_URL, with the old REGISTRATION_DB_DSN as a fallback."""

from __future__ import annotations

import logging

import pytest

from agentnet import settings
from agentnet.settings import database_configured, database_url

URL = "postgresql://svc:new-secret@db.example:5432/registration"
OLD_URL = "postgresql://svc:old-secret@old.example:5432/registration"


@pytest.fixture(autouse=True)
def _fresh(caplog):
    settings.reset_warnings()
    caplog.set_level(logging.DEBUG, logger="agentnet.settings")
    yield
    settings.reset_warnings()


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "agentnet.settings"]


def test_new_name(caplog):
    assert database_url({"DATABASE_URL": URL}) == URL
    assert database_configured({"DATABASE_URL": URL})
    assert _warnings(caplog) == []


def test_old_name_still_works_and_warns_once(caplog):
    env = {"REGISTRATION_DB_DSN": OLD_URL}
    assert database_url(env) == OLD_URL
    assert database_configured(env)
    assert database_url(env) == OLD_URL
    assert _warnings(caplog) == ["REGISTRATION_DB_DSN is deprecated; use DATABASE_URL"]


def test_both_names_with_the_same_value_is_quiet(caplog):
    assert database_url({"DATABASE_URL": URL, "REGISTRATION_DB_DSN": URL}) == URL
    assert _warnings(caplog) == []


def test_both_names_with_different_values_uses_the_new_one_and_warns_once(caplog):
    env = {"DATABASE_URL": URL, "REGISTRATION_DB_DSN": OLD_URL}
    assert database_url(env) == URL
    assert database_url(env) == URL
    assert _warnings(caplog) == [
        "DATABASE_URL and REGISTRATION_DB_DSN are both set, to different values; using DATABASE_URL"
    ]


def test_empty_counts_as_unset():
    # An empty DATABASE_URL must not hide a real old value (the chat app would stop
    # requiring login).
    assert database_url({"DATABASE_URL": "", "REGISTRATION_DB_DSN": OLD_URL}) == OLD_URL
    assert database_url({"DATABASE_URL": URL, "REGISTRATION_DB_DSN": ""}) == URL
    assert database_url({"DATABASE_URL": "", "REGISTRATION_DB_DSN": ""}) is None
    assert not database_configured({"DATABASE_URL": ""})


def test_neither_set():
    assert database_url({}) is None
    assert not database_configured({})


def test_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("REGISTRATION_DB_DSN", OLD_URL)
    assert database_url() == OLD_URL
    monkeypatch.setenv("DATABASE_URL", URL)
    assert database_url() == URL
    assert database_configured()


def test_the_value_is_never_logged(caplog):
    database_url({"REGISTRATION_DB_DSN": OLD_URL})
    settings.reset_warnings()
    database_url({"DATABASE_URL": URL, "REGISTRATION_DB_DSN": OLD_URL})
    assert len(_warnings(caplog)) == 2
    assert "secret" not in caplog.text
    assert "example" not in caplog.text
