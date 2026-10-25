"""The database address: where the registration service reads it from, and that the
agents it starts never get it."""

from __future__ import annotations

import importlib.util
import os

import pytest

from agentnet import settings
from registration_service import backend
from registration_service.backend import _REPO_ROOT, SubprocessBackend
from registration_service.db import _dsn
from registration_service.models import ProxyConfig

URL = "postgresql://regsvc:pw@db.example:5432/registration"
OLD_URL = "postgresql://postgres@localhost:5432/registration"
_PG_VARS = ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("DATABASE_URL", "REGISTRATION_DB_DSN", *_PG_VARS):
        monkeypatch.delenv(name, raising=False)
    settings.reset_warnings()


def test_dsn_reads_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    assert _dsn() == URL


def test_dsn_still_reads_the_old_name(monkeypatch):
    monkeypatch.setenv("REGISTRATION_DB_DSN", OLD_URL)
    assert _dsn() == OLD_URL


def test_dsn_prefers_database_url_over_the_old_name(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("REGISTRATION_DB_DSN", OLD_URL)
    assert _dsn() == URL


def test_dsn_falls_back_to_the_pg_variables(monkeypatch):
    assert _dsn() == "postgresql://postgres:postgres@localhost:5432/registration"
    for name, value in {"PGHOST": "pg", "PGPORT": "6543", "PGUSER": "u", "PGPASSWORD": "p",
                        "PGDATABASE": "d"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_URL", "")  # empty counts as unset
    assert _dsn() == "postgresql://u:p@pg:6543/d"


class _Spawned(Exception):
    pass


async def test_subprocess_agents_do_not_get_the_database_address(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("REGISTRATION_DB_DSN", OLD_URL)
    monkeypatch.setenv("AGENT_ENV_PROBE", "kept")
    seen = {}

    async def fake_exec(*args, env, **kwargs):
        seen.update(env)
        raise _Spawned

    monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(_Spawned):
        await SubprocessBackend().spawn(
            agent_id="a1", name="Env", description="d", handler_type="proxy",
            config=ProxyConfig(upstream_url="http://127.0.0.1:9/"), port=19999,
        )
    assert "DATABASE_URL" not in seen
    assert "REGISTRATION_DB_DSN" not in seen
    assert seen["AGENT_ENV_PROBE"] == "kept"  # the rest is still inherited
    assert "AGENT_CONFIG_JSON" in seen


def test_cluster_agents_do_not_get_the_database_address(monkeypatch):
    path = _REPO_ROOT / "deployment" / "spawn_agent_loop.py"
    spec = importlib.util.spec_from_file_location("spawn_agent_loop", path)
    loop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loop)
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("REGISTRATION_DB_DSN", OLD_URL)
    monkeypatch.setenv("AGENT_ENV_PROBE", "kept")

    env = loop._agent_env({"AGENT_CONFIG_JSON": "{}", "DATABASE_URL": URL})
    assert "DATABASE_URL" not in env
    assert "REGISTRATION_DB_DSN" not in env
    assert env["AGENT_ENV_PROBE"] == "kept"
    assert env["AGENT_CONFIG_JSON"] == "{}"
    assert env["PATH"] == os.environ["PATH"]
    # The loop runs on the host without the repo's packages, so it keeps its own
    # copy of the names; it must stay in step with agentnet.settings.
    assert set(loop._AGENT_ENV_EXCLUDED) == {settings.DATABASE_URL, *settings.OLD_DATABASE_NAMES}
