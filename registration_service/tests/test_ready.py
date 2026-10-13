"""GET /ready: the planner's readiness shape, per dependency the service uses.

``{"ready": bool, "checks": {"db": {...}, "directory": {...}}}``, each check with
``status`` in ok | not_configured | error (plus ``error`` when it failed); 200
unless a check is in error, then 503. /health stays a cheap liveness probe.
"""

from __future__ import annotations

import registration_service.db as db_mod
from registration_service.tests.agent_stubs import write_fake_dirctl


def _real_supervisor_env(monkeypatch, *, server_addr: str | None, dirctl: str | None = None) -> None:
    monkeypatch.setenv("FAKE_SUPERVISOR", "0")
    for name in ("ADS_SERVER_ADDR", "ADS_URL", "ADS_SERVER_ADDRESS", "DIRECTORY_CLIENT_SERVER_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    if server_addr:
        monkeypatch.setenv("ADS_SERVER_ADDR", server_addr)
    if dirctl:
        monkeypatch.setenv("DIRCTL_BIN", dirctl)


def _assert_shape(body: dict) -> None:
    assert set(body) == {"ready", "checks"}
    assert set(body["checks"]) == {"db", "directory"}
    for check in body["checks"].values():
        assert check["status"] in ("ok", "not_configured", "error")
        assert ("error" in check) == (check["status"] == "error")


async def test_fake_mode_is_ready_and_directory_not_configured(client) -> None:
    r = await client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    _assert_shape(body)
    assert body["ready"] is True
    assert body["checks"]["db"] == {"status": "ok", "backend": "fake"}
    # FakeSupervisor never publishes, so the directory is not a dependency.
    assert body["checks"]["directory"]["status"] == "not_configured"


async def test_directory_without_address_is_not_configured(client, monkeypatch) -> None:
    _real_supervisor_env(monkeypatch, server_addr=None)
    r = await client.get("/ready")
    assert r.status_code == 200
    assert r.json()["checks"]["directory"] == {"status": "not_configured"}


async def test_directory_ok_through_dirctl(client, monkeypatch, tmp_path) -> None:
    dirctl, _ = write_fake_dirctl(tmp_path)
    _real_supervisor_env(monkeypatch, server_addr="fake-dir:8888", dirctl=str(dirctl))
    r = await client.get("/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_shape(body)
    assert body["checks"]["directory"] == {"status": "ok", "server_addr": "fake-dir:8888"}


async def test_unreachable_directory_fails_readiness(client, monkeypatch, tmp_path) -> None:
    _real_supervisor_env(
        monkeypatch, server_addr="fake-dir:8888", dirctl=str(tmp_path / "no-such-dirctl")
    )
    r = await client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    _assert_shape(body)
    assert body["ready"] is False
    assert body["checks"]["db"]["status"] == "ok"
    directory = body["checks"]["directory"]
    assert directory["status"] == "error"
    assert "dirctl not found" in directory["error"]


async def test_db_not_initialised_fails_readiness(client, monkeypatch) -> None:
    monkeypatch.setattr(db_mod, "_pool", None)
    r = await client.get("/ready")
    assert r.status_code == 503
    db_check = r.json()["checks"]["db"]
    assert db_check["status"] == "error"
    assert "not initialised" in db_check["error"]
    # Liveness does not depend on anything.
    assert (await client.get("/health")).json() == {"status": "ok"}
