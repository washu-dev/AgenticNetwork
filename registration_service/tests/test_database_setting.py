"""The database address: where the registration service reads it from, and that the
agents it starts, and the steps that install their code, never get it."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from agentnet import settings
from registration_service import backend
from registration_service.backend import _REPO_ROOT, SubprocessBackend
from registration_service.db import _dsn
from registration_service.models import ProxyConfig

URL = "postgresql://regsvc:pw@db.example:5432/registration"
OLD_URL = "postgresql://postgres@localhost:5432/registration"
# What _dsn builds the address from when no URL is set.
_PG_VARS = ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")
# Every name that can carry the address, as the service would have them set.
_ADDRESS = {"DATABASE_URL": URL, "REGISTRATION_DB_DSN": OLD_URL,
            **{name: "pg" for name in settings.PG_ADDRESS_NAMES}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("DATABASE_URL", "REGISTRATION_DB_DSN", *settings.PG_ADDRESS_NAMES):
        monkeypatch.delenv(name, raising=False)
    settings.reset_warnings()


def _set_address(monkeypatch):
    for name, value in _ADDRESS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AGENT_ENV_PROBE", "kept")


def _assert_no_address(env):
    assert [name for name in _ADDRESS if name in env] == []
    assert env["AGENT_ENV_PROBE"] == "kept"  # the rest is still inherited


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


def test_the_address_names_cover_what_dsn_reads():
    assert {"DATABASE_URL", "REGISTRATION_DB_DSN", *_PG_VARS} <= set(backend._AGENT_ENV_EXCLUDED)


class _Spawned(Exception):
    pass


async def test_subprocess_agents_do_not_get_the_database_address(monkeypatch):
    _set_address(monkeypatch)
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
    _assert_no_address(seen)
    assert "AGENT_CONFIG_JSON" in seen


async def test_install_steps_do_not_get_the_database_address(monkeypatch, tmp_path):
    # An install step (pip on an upload's project or requirements) runs the
    # agent's own build code, so it starts from the same environment as the agent.
    _set_address(monkeypatch)
    out = tmp_path / "env.json"
    dump = f"import json, os; open({str(out)!r}, 'w').write(json.dumps(dict(os.environ)))"
    await backend._run([sys.executable, "-c", dump])
    _assert_no_address(json.loads(out.read_text()))


def test_cluster_agents_do_not_get_the_database_address(monkeypatch):
    path = _REPO_ROOT / "deployment" / "spawn_agent_loop.py"
    spec = importlib.util.spec_from_file_location("spawn_agent_loop", path)
    loop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loop)
    _set_address(monkeypatch)

    env = loop._agent_env({"AGENT_CONFIG_JSON": "{}", "DATABASE_URL": URL})
    _assert_no_address(env)
    assert env["AGENT_CONFIG_JSON"] == "{}"
    assert env["PATH"] == os.environ["PATH"]
    # The loop runs on the host without the repo's packages, so it keeps its own
    # copy of the names; it must stay in step with SubprocessBackend's.
    assert set(loop._AGENT_ENV_EXCLUDED) == set(backend._AGENT_ENV_EXCLUDED)


@pytest.mark.parametrize("dotenv, secrets, expected", [
    ("REGISTRATION_DB_DSN=" + OLD_URL, "", OLD_URL),  # a live .env may still use the old name
    ("DATABASE_URL=" + URL, "", URL),
    ("DATABASE_URL=" + OLD_URL, "DATABASE_URL=" + URL, URL),  # the secrets file is sourced last
    ("", "", ""),  # neither: the services fall back to the PG* variables
])
def test_submit_sbatch_takes_the_address_from_its_files_under_both_names(
    tmp_path, dotenv, secrets, expected
):
    # sbatch passes the submitting shell's environment on; a database address set
    # there must not reach the services. Old images read only the old name, so
    # both names carry the one address.
    sbatch = (_REPO_ROOT / "deployment" / "submit.sbatch").read_text()
    start = sbatch.index("# Load env vars")
    end = sbatch.index("\nfi\n", sbatch.index('export DATABASE_URL REGISTRATION_DB_DSN="$DATABASE_URL"')) + 4
    (tmp_path / ".env").write_text(dotenv + "\n")
    (tmp_path / "secrets.env").write_text(secrets + "\n")
    shell = {
        "PATH": os.environ["PATH"],
        "DATABASE_URL": "postgresql://other.example/project",
        "REGISTRATION_DB_DSN": "postgresql://other.example/old",
        "ENV_FILE": str(tmp_path / ".env"),
        "SECRETS_FILE": str(tmp_path / "secrets.env"),
    }
    # What a step the script starts sees: only exported values.
    show = """sh -c 'printf "%s|%s" "${DATABASE_URL-}" "${REGISTRATION_DB_DSN-}"'"""
    out = subprocess.run(
        ["bash", "-c", "{\n" + sbatch[start:end] + "} >/dev/null\n" + show],
        env=shell, capture_output=True, text=True, check=True,
    )
    assert out.stdout == f"{expected}|{expected}"
