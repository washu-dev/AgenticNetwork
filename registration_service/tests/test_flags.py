"""FAKE_DB / FAKE_SUPERVISOR parsing and what each combination selects."""

from __future__ import annotations

import pytest

import registration_service.supervisor as sup_mod
from registration_service.fake_supervisor import FakeSupervisor
from registration_service.flags import env_flag, fake_db_enabled, fake_supervisor_enabled
from registration_service.server import _require_admin


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "On", " 1 "])
def test_truthy_values(monkeypatch, value) -> None:
    monkeypatch.setenv("SOME_FLAG", value)
    assert env_flag("SOME_FLAG") is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "2", "fake"])
def test_other_values_are_false(monkeypatch, value) -> None:
    monkeypatch.setenv("SOME_FLAG", value)
    assert env_flag("SOME_FLAG", default=True) is False


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_or_empty_uses_default(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv("SOME_FLAG", raising=False)
    else:
        monkeypatch.setenv("SOME_FLAG", value)
    assert env_flag("SOME_FLAG") is False
    assert env_flag("SOME_FLAG", default=True) is True


def test_fake_db_zero_is_off(monkeypatch) -> None:
    """Regression: any non-empty FAKE_DB used to enable fake mode, even "0"."""
    monkeypatch.setenv("FAKE_DB", "0")
    assert fake_db_enabled() is False


@pytest.mark.parametrize(
    "fake_db,fake_sup,expected",
    [
        ("1", None, True),      # FAKE_DB alone keeps the no-spawn mode
        ("0", None, False),
        (None, None, False),
        ("1", "0", False),      # native dev stack: in-memory DB, real supervisor
        ("true", "false", False),
        ("0", "1", True),
    ],
)
def test_fake_supervisor_defaults_to_fake_db(monkeypatch, fake_db, fake_sup, expected) -> None:
    for name, value in (("FAKE_DB", fake_db), ("FAKE_SUPERVISOR", fake_sup)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert fake_supervisor_enabled() is expected


@pytest.mark.parametrize("fake_sup,cls", [("1", FakeSupervisor), ("0", sup_mod.Supervisor)])
def test_get_supervisor_follows_fake_supervisor(monkeypatch, fake_sup, cls) -> None:
    monkeypatch.setenv("FAKE_DB", "1")
    monkeypatch.setenv("FAKE_SUPERVISOR", fake_sup)
    monkeypatch.setattr(sup_mod, "_supervisor", None)
    try:
        assert type(sup_mod.get_supervisor()) is cls
    finally:
        sup_mod._supervisor = None


class _Req:
    headers: dict = {}


@pytest.mark.parametrize("value,open_", [("1", True), ("true", True), ("0", False)])
def test_admin_dev_bypass_uses_the_same_parsing(monkeypatch, value, open_) -> None:
    monkeypatch.setenv("FAKE_DB", value)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    if open_:
        _require_admin(_Req())
    else:
        with pytest.raises(Exception, match="Admin access required"):
            _require_admin(_Req())
