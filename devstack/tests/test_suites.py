"""`scripts/stack test` (devstack/suites.py), with fake interpreters that only record how
they were called, so no real suite runs here."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from devstack import stack, suites

# Stands in for both venvs' python: answers stack.venv_report, records every other call
# (argv, env, cwd, start and end time), and passes, fails or hangs as config.json says.
FAKE_PYTHON = """\
import json, os, signal, sys, time
from pathlib import Path

here = Path(__file__).parent
role, args = sys.argv[1], sys.argv[2:]
config = json.loads((here / "config.json").read_text())
if args[:2] == ["-I", "-c"]:
    print(json.dumps({"python": "3.11.9", "packages": config["packages"][role]}))
    sys.exit(0)
started = time.time()
if args[:2] == ["-m", "coverage"]:
    print("TOTAL      10      2    80%")
    code = 0
else:
    path = args[2]
    if path in config["hang"]:
        if config["ignore_term"]:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    time.sleep(config["sleep"].get(path, 0))
    code = config["exit"].get(path, 0)
    print(f"{path} output")
    print("1 failed in 0.01s" if code else "3 passed in 0.01s")
call = {"role": role, "args": args, "env": dict(os.environ), "cwd": os.getcwd(), "started": started, "ended": time.time()}
with (here / "calls.jsonl").open("a") as log:
    log.write(json.dumps(call) + "\\n")
sys.exit(code)
"""
PACKAGES = {"pytest": "9.0.3", "pytest-asyncio": "1.4.0", "pytest-cov": "7.1.0"}


@dataclass
class Fake:
    root: Path
    chat: Path
    registration: Path

    def configure(self, **config: Any) -> None:
        defaults = {
            "packages": {"chat": dict(PACKAGES), "registration": dict(PACKAGES)},
            "exit": {}, "hang": [], "ignore_term": False, "sleep": {},
        }
        (self.root / "config.json").write_text(json.dumps({**defaults, **config}), encoding="utf-8")

    def calls(self, tool: str = "pytest") -> list[dict[str, Any]]:
        log = self.root / "calls.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return [call for call in map(json.loads, lines) if call["args"][:2] == ["-m", tool]]

    def ran(self) -> list[tuple[str, str]]:
        """(role, path) of each pytest call, in order."""
        return [(call["role"], call["args"][2]) for call in self.calls()]


@pytest.fixture
def fake(tmp_path, monkeypatch) -> Fake:
    script = tmp_path / "fake_python.py"
    script.write_text(FAKE_PYTHON, encoding="utf-8")
    pythons = {}
    for role in ("chat", "registration"):
        wrapper = tmp_path / f"{role}-python"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {role} "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        pythons[role] = wrapper
    monkeypatch.setenv("CHAT_APP_PYTHON", str(pythons["chat"]))
    monkeypatch.setenv("REGISTRATION_PYTHON", str(pythons["registration"]))
    for name in ("PLANNER_PYTHON", "PIPELINE_PORT_OFFSET", "PLANNER_TEST_DATABASE_DSN", "DEVSTACK_PASSTHROUGH_ENV"):
        monkeypatch.delenv(name, raising=False)
    which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/lsof" if name == "lsof" else which(name, *a, **k))
    monkeypatch.setattr(stack, "cmd_down", lambda args: pytest.fail("down ran"))
    result = Fake(tmp_path, pythons["chat"], pythons["registration"])
    result.configure()
    return result


def json_out(capsys) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_selection_by_group_and_suite():
    names = lambda picked: [s.name for s in picked]  # noqa: E731
    assert suites.select([]) == list(suites.SUITES)
    assert names(suites.select(["pipeline"])) == ["full-chain", "scenarios"]
    assert names(suites.select(["unit"])) == [
        "chat_app", "registration_service", "agent_skeleton", "agentnet", "agentnet", "devstack",
    ]
    assert [s.venv for s in suites.select(["agentnet"])] == ["registration", "chat-app"]
    assert [s.venv for s in suites.select(["agentnet[chat-app]"])] == ["chat-app"]
    # Table order, each suite once.
    assert names(suites.select(["scenarios", "chat_app", "pipeline"])) == ["chat_app", "full-chain", "scenarios"]


def test_unknown_suite_is_a_usage_error(fake, capsys):
    assert stack.main(["test", "chat_app", "nope"]) == stack.EXIT_USAGE
    assert "unknown suite 'nope'" in capsys.readouterr().err
    assert stack.main(["test", "nope", "--json"]) == stack.EXIT_USAGE
    error = json_out(capsys)
    assert error["ok"] is False and error["error"]["exit_code"] == stack.EXIT_USAGE
    assert fake.ran() == []


def test_each_suite_runs_alone_with_its_interpreter(fake, capsys):
    assert stack.main(["test", "unit"]) == stack.EXIT_OK
    assert fake.ran() == [
        ("chat", "chat_app/tests"),
        ("registration", "registration_service/tests"),
        ("registration", "agent_skeleton/tests"),
        ("registration", "agentnet/tests"),
        ("chat", "agentnet/tests"),
        ("registration", "devstack/tests"),
    ]
    for call in fake.calls():
        assert call["args"][3:] == ["-q", "-p", "no:cacheprovider"]
        assert Path(call["cwd"]).resolve() == stack.REPO
    out = capsys.readouterr().out
    assert "devstack test: all 6 passed" in out
    assert "agentnet[registration]" in out and "agentnet[chat-app]" in out


def test_python_replaces_both_venvs_and_runs_agentnet_once(fake, capsys):
    assert stack.main(["test", "agentnet", "--python", str(fake.chat), "--list", "--json"]) == stack.EXIT_OK
    assert json_out(capsys) == [{"suite": "agentnet", "path": "agentnet/tests", "python": str(fake.chat), "pipeline": False}]
    assert fake.ran() == []  # --list runs nothing
    assert stack.main(["test", "agentnet", "registration_service", "--python", str(fake.chat)]) == stack.EXIT_OK
    assert fake.ran() == [("chat", "registration_service/tests"), ("chat", "agentnet/tests")]


def test_args_after_double_dash_go_to_pytest(fake):
    assert stack.main(["test", "devstack", "--", "-k", "unit", "-x"]) == stack.EXIT_OK
    [call] = fake.calls()
    assert call["args"][2:] == ["devstack/tests", "-q", "-p", "no:cacheprovider", "-k", "unit", "-x"]


def test_pipeline_files_get_their_own_ports_and_state_dirs(fake, monkeypatch):
    monkeypatch.setenv("PIPELINE_PORT_OFFSET", "21000")
    assert stack.main(["test", "pipeline"]) == stack.EXIT_OK
    assert fake.ran() == [("registration", "tests/pipeline/test_full_chain.py"),
                          ("registration", "tests/pipeline/test_scenarios.py")]
    envs = [call["env"] for call in fake.calls()]
    assert [(env["PIPELINE_PORT_OFFSET"], env["PIPELINE_STATE_DIR"]) for env in envs] == [
        ("21000", str(stack.REPO / ".devstack-pipeline")),
        ("21100", str(stack.REPO / ".devstack-scenarios")),
    ]
    # The stack it starts runs its services with the interpreters preflight checked.
    assert all(env["CHAT_APP_PYTHON"] == str(fake.chat) for env in envs)
    monkeypatch.setenv("PIPELINE_PORT_OFFSET", "60000")
    assert stack.main(["test", "scenarios"]) == stack.EXIT_USAGE


def test_suites_get_a_clean_environment(fake, monkeypatch):
    for name, value in {
        "FAKE_DB": "0", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": "/elsewhere",
        "DATABASE_URL": "postgresql://db/x", "REGISTRATION_DB_DSN": "postgresql://db/x",
        "PIPELINE_STATE_DIR": "/elsewhere",
        "PLANNER_TEST_DATABASE_DSN": "postgresql://test/planner",
    }.items():
        monkeypatch.setenv(name, value)
    assert stack.main(["test", "devstack"]) == stack.EXIT_OK
    env = fake.calls()[0]["env"]
    for name in ("FAKE_DB", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTHONPATH", "DATABASE_URL",
                 "REGISTRATION_DB_DSN", "PIPELINE_STATE_DIR", "PIPELINE_PORT_OFFSET", "REGISTRATION_PYTHON"):
        assert name not in env
    assert env["PLANNER_TEST_DATABASE_DSN"] == "postgresql://test/planner"  # opts in to the Postgres tests
    assert str(stack.DIRCTL_SHIM.parent) not in env["PATH"].split(os.pathsep)
    assert env["HOME"] == os.environ["HOME"]


def test_a_failed_suite_exits_7_after_the_rest_ran(fake, capsys):
    fake.configure(exit={"agentnet/tests": 1})
    assert stack.main(["test", "unit"]) == stack.EXIT_TESTS
    assert len(fake.ran()) == 6
    out = capsys.readouterr().out
    assert "devstack test: 2 of 6 failed" in out
    rows = [line.split()[:2] for line in out.splitlines()]
    assert ["agentnet[chat-app]", "FAILED"] in rows and ["chat_app", "ok"] in rows and "1 failed in 0.01s" in out
    fake.configure(exit={"tests/pipeline/test_scenarios.py": 1})
    assert stack.main(["test", "chat_app", "scenarios", "--json"]) == stack.EXIT_TESTS
    summary = json_out(capsys)
    assert summary["ok"] is False and summary["failed"] == ["scenarios"]
    assert summary["suites"][1]["logs"] == str(stack.REPO / ".devstack-scenarios" / "logs")


@pytest.mark.parametrize("case", ["no interpreter", "no pytest-asyncio", "no pytest-cov", "no lsof"])
def test_preflight_problems_exit_3_before_anything_runs(fake, monkeypatch, capsys, case):
    argv = ["test", "registration_service"]
    if case == "no interpreter":
        monkeypatch.setenv("REGISTRATION_PYTHON", str(fake.root / "absent"))
        expected = "set REGISTRATION_PYTHON"
    elif case == "no pytest-asyncio":
        fake.configure(packages={"chat": PACKAGES, "registration": {"pytest": "9.0.3"}})
        expected = "lacks pytest-asyncio"
        # The other suites in that venv need only pytest.
        assert stack.main(["test", "agent_skeleton"]) == stack.EXIT_OK
    elif case == "no pytest-cov":
        fake.configure(packages={"chat": PACKAGES, "registration": {"pytest": "9.0.3", "pytest-asyncio": "1.4.0"}})
        argv.append("--cov")
        expected = "lacks pytest-cov"
        assert stack.main(["test", "registration_service"]) == stack.EXIT_OK  # fine without --cov
    else:
        monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
        argv = ["test", "full-chain"]
        expected = "lsof not found"
    ran = fake.ran()
    assert stack.main(argv) == stack.EXIT_PREFLIGHT
    assert expected in capsys.readouterr().err
    assert fake.ran() == ran


def test_cov_measures_each_unit_suite_into_its_own_file(fake, capsys):
    assert stack.main(["test", "agentnet", "full-chain", "--cov", "--python", str(fake.registration), "--json"]) == 0
    unit, pipeline = fake.calls()
    assert unit["args"][-2:] == ["--cov=agentnet", "--cov-report="]
    assert unit["env"]["COVERAGE_FILE"] == str(stack.REPO / ".coverage.agentnet")
    assert not any(arg.startswith("--cov") for arg in pipeline["args"])  # its code runs in subprocesses
    [report] = fake.calls("coverage")
    assert report["args"] == ["-m", "coverage", "report", "--omit=*/tests/*"]
    assert report["env"]["COVERAGE_FILE"] == unit["env"]["COVERAGE_FILE"]
    assert json_out(capsys)["suites"][0]["coverage"] == "80%"


def test_parallel_runs_unit_suites_at_once_then_the_pipeline(fake, capsys):
    fake.configure(sleep={"agentnet/tests": 1.0})
    assert stack.main(["test", "agentnet", "full-chain", "--parallel"]) == stack.EXIT_OK
    calls = {(c["role"], c["args"][2]): c for c in fake.calls()}
    first, second = calls["registration", "agentnet/tests"], calls["chat", "agentnet/tests"]
    assert max(first["started"], second["started"]) < min(first["ended"], second["ended"])
    assert calls["registration", "tests/pipeline/test_full_chain.py"]["started"] >= max(first["ended"], second["ended"])
    out = capsys.readouterr().out
    for label, python in (("agentnet[registration]", fake.registration), ("agentnet[chat-app]", fake.chat)):
        assert f"devstack test: {label} ({python})\nagentnet/tests output\n" in out  # each in one piece


def test_a_hung_suite_is_stopped(fake, monkeypatch, capsys):
    monkeypatch.setattr(suites, "KILL_GRACE", 0.5)
    fake.configure(hang=["devstack/tests"], ignore_term=True)  # needs SIGKILL
    assert stack.main(["test", "devstack", "--timeout", "0.5", "--json"]) == stack.EXIT_TESTS
    [result] = json_out(capsys)["suites"]
    assert result["timed_out"] is True and result["ok"] is False and result["exit_code"] == -9
    assert result["summary"] == "stopped after 0.5 s"


def test_a_hung_pipeline_file_is_stopped_and_its_stack_brought_down(fake, monkeypatch, capsys):
    downs: list[str] = []
    monkeypatch.setattr(stack, "cmd_down", lambda args: downs.append(args.state_dir) or stack.EXIT_OK)
    fake.configure(hang=["tests/pipeline/test_full_chain.py"])
    assert stack.main(["test", "pipeline", "--timeout", "0.5", "--json"]) == stack.EXIT_TESTS
    full_chain, scenarios = json_out(capsys)["suites"]
    assert full_chain["timed_out"] is True and full_chain["exit_code"] == -15  # SIGTERM was enough
    assert scenarios["ok"] is True  # the next file still ran
    assert downs == [str(stack.REPO / ".devstack-pipeline")]
