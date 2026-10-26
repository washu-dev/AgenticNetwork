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

# Stands in for an interpreter: answers stack.venv_report, records every other call
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
    print({0: "3 passed in 0.01s", 5: "3 deselected in 0.01s"}.get(code, "1 failed in 0.01s"))
call = {"role": role, "args": args, "env": dict(os.environ), "cwd": os.getcwd(), "started": started, "ended": time.time()}
with (here / "calls.jsonl").open("a") as log:
    log.write(json.dumps(call) + "\\n")
sys.exit(code)
"""
PACKAGES = {"pytest": "9.0.3", "pytest-asyncio": "1.4.0", "pytest-cov": "7.1.0"}


@dataclass
class Fake:
    root: Path
    dev: Path  # stands in for the dev venv's python
    other: Path  # another interpreter, for --python and the service overrides

    def configure(self, **config: Any) -> None:
        defaults = {
            "packages": {"dev": dict(PACKAGES), "other": dict(PACKAGES)},
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
    for role in ("dev", "other"):
        wrapper = tmp_path / f"{role}-python"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {role} "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        pythons[role] = wrapper
    monkeypatch.setattr(stack, "DEV_VENV_PYTHON", pythons["dev"])
    for name in ("CHAT_APP_PYTHON", "PLANNER_PYTHON", "REGISTRATION_PYTHON", "PIPELINE_PORT_OFFSET",
                 "PLANNER_TEST_DATABASE_DSN", "DEVSTACK_PASSTHROUGH_ENV"):
        monkeypatch.delenv(name, raising=False)
    which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/lsof" if name == "lsof" else which(name, *a, **k))
    monkeypatch.setattr(stack, "cmd_down", lambda args: pytest.fail("down ran"))
    result = Fake(tmp_path, pythons["dev"], pythons["other"])
    result.configure()
    return result


def json_out(capsys) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_selection_by_group_and_suite():
    names = lambda picked: [s.name for s in picked]  # noqa: E731
    assert suites.select([]) == list(suites.SUITES)
    assert names(suites.select(["pipeline"])) == ["full-chain", "scenarios"]
    assert names(suites.select(["unit"])) == ["chat_app", "registration_service", "agent_skeleton", "agentnet", "devstack"]
    assert names(suites.select(["agentnet"])) == ["agentnet"]  # once: one venv runs both services
    # Table order, each suite once.
    assert names(suites.select(["scenarios", "chat_app", "pipeline"])) == ["chat_app", "full-chain", "scenarios"]


def test_unknown_suite_is_a_usage_error(fake, capsys):
    assert stack.main(["test", "chat_app", "nope"]) == stack.EXIT_USAGE
    assert "unknown suite 'nope'" in capsys.readouterr().err
    assert stack.main(["test", "nope", "--json"]) == stack.EXIT_USAGE
    error = json_out(capsys)
    assert error["ok"] is False and error["error"]["exit_code"] == stack.EXIT_USAGE
    assert fake.ran() == []


def test_each_suite_runs_alone_with_the_dev_venv(fake, monkeypatch, capsys):
    # The service overrides pick the stack's interpreters, not the tests'.
    monkeypatch.setenv("CHAT_APP_PYTHON", str(fake.other))
    monkeypatch.setenv("REGISTRATION_PYTHON", str(fake.other))
    assert stack.main(["test", "unit"]) == stack.EXIT_OK
    assert fake.ran() == [
        ("dev", "chat_app/tests"),
        ("dev", "registration_service/tests"),
        ("dev", "agent_skeleton/tests"),
        ("dev", "agentnet/tests"),
        ("dev", "devstack/tests"),
    ]
    for call in fake.calls():
        assert call["args"][3:] == ["-q", "-p", "no:cacheprovider"]
        assert Path(call["cwd"]).resolve() == stack.REPO
    out = capsys.readouterr().out
    assert "devstack test: all 5 passed" in out


def test_python_replaces_the_dev_venv(fake, capsys):
    assert stack.main(["test", "agentnet", "--python", str(fake.other), "--list", "--json"]) == stack.EXIT_OK
    assert json_out(capsys) == [{"suite": "agentnet", "path": "agentnet/tests", "python": str(fake.other), "pipeline": False}]
    assert fake.ran() == []  # --list runs nothing
    assert stack.main(["test", "agentnet", "registration_service", "--python", str(fake.other)]) == stack.EXIT_OK
    assert fake.ran() == [("other", "registration_service/tests"), ("other", "agentnet/tests")]


def test_args_after_double_dash_go_to_pytest(fake):
    assert stack.main(["test", "devstack", "--", "-k", "unit", "-x"]) == stack.EXIT_OK
    [call] = fake.calls()
    assert call["args"][2:] == ["devstack/tests", "-q", "-p", "no:cacheprovider", "-k", "unit", "-x"]


def test_pipeline_files_get_their_own_ports_and_state_dirs(fake, monkeypatch):
    monkeypatch.setenv("PIPELINE_PORT_OFFSET", "21000")
    monkeypatch.setenv("CHAT_APP_PYTHON", str(fake.other))
    assert stack.main(["test", "pipeline"]) == stack.EXIT_OK
    assert fake.ran() == [("dev", "tests/pipeline/test_full_chain.py"), ("dev", "tests/pipeline/test_scenarios.py")]
    envs = [call["env"] for call in fake.calls()]
    assert [(env["PIPELINE_PORT_OFFSET"], env["PIPELINE_STATE_DIR"]) for env in envs] == [
        ("21000", str(stack.REPO / ".devstack-pipeline")),
        ("21100", str(stack.REPO / ".devstack-scenarios")),
    ]
    # The stack it starts runs its services with the interpreters preflight checked.
    assert all(env["CHAT_APP_PYTHON"] == str(fake.other) for env in envs)
    monkeypatch.setenv("PIPELINE_PORT_OFFSET", "60000")
    assert stack.main(["test", "scenarios"]) == stack.EXIT_USAGE


def test_suites_get_a_clean_environment(fake, monkeypatch):
    for name, value in {
        "FAKE_DB": "0", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": "/elsewhere",
        "DATABASE_URL": "postgresql://db/x", "REGISTRATION_DB_DSN": "postgresql://db/x",
        "PIPELINE_STATE_DIR": "/elsewhere", "REGISTRATION_PYTHON": str(fake.other),
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
    assert len(fake.ran()) == 5
    out = capsys.readouterr().out
    assert "devstack test: 1 of 5 failed" in out
    rows = [line.split()[:2] for line in out.splitlines()]
    assert ["agentnet", "FAILED"] in rows and ["chat_app", "ok"] in rows and "1 failed in 0.01s" in out
    fake.configure(exit={"tests/pipeline/test_scenarios.py": 1})
    assert stack.main(["test", "chat_app", "scenarios", "--json"]) == stack.EXIT_TESTS
    summary = json_out(capsys)
    assert summary["ok"] is False and summary["failed"] == ["scenarios"]
    assert summary["verdict"] == "1 of 2 failed"
    assert summary["suites"][1]["logs"] == str(stack.REPO / ".devstack-scenarios" / "logs")


def test_a_suite_where_pytest_args_select_no_test_is_none_not_failed(fake, capsys):
    fake.configure(exit={"agentnet/tests": 5, "tests/pipeline/test_full_chain.py": 5})  # pytest: no test ran
    assert stack.main(["test", "agentnet", "devstack", "full-chain", "--", "-k", "sweep"]) == stack.EXIT_OK
    out = capsys.readouterr().out
    assert "devstack test: 1 passed, 2 selected no test" in out
    rows = [line.split()[:2] for line in out.splitlines()]
    assert ["agentnet", "none"] in rows and ["full-chain", "none"] in rows and ["devstack", "ok"] in rows
    assert "stack logs" not in out  # nothing to debug
    assert stack.main(["test", "agentnet", "devstack", "--json", "--", "-k", "sweep"]) == stack.EXIT_OK
    summary = json_out(capsys)
    assert summary["ok"] is True and summary["failed"] == []
    assert summary["verdict"] == "1 passed, 1 selected no test"
    assert [(s["result"], s["exit_code"]) for s in summary["suites"]] == [("none", 5), ("ok", 0)]
    # Arguments that select no test anywhere still fail, as a typo in -k would.
    assert stack.main(["test", "agentnet", "--", "-k", "sweeep"]) == stack.EXIT_TESTS
    out = capsys.readouterr().out
    assert "devstack test: no test selected" in out and ["agentnet", "none"] in [line.split()[:2] for line in out.splitlines()]
    # The JSON summary says why too: no suite failed, so "failed" alone can't.
    assert stack.main(["test", "agentnet", "--json", "--", "-k", "sweeep"]) == stack.EXIT_TESTS
    summary = json_out(capsys)
    assert summary["ok"] is False and summary["failed"] == []
    assert summary["verdict"] == "no test selected"
    # Without pytest arguments, a suite that collects nothing is broken.
    assert stack.main(["test", "agentnet", "devstack", "--json"]) == stack.EXIT_TESTS
    summary = json_out(capsys)
    assert summary["failed"] == ["agentnet"] and summary["suites"][0]["result"] == "failed"


@pytest.mark.parametrize(
    "case", ["no dev venv", "no service interpreter", "no pytest-asyncio", "no pytest-cov", "no lsof"],
)
def test_preflight_problems_exit_3_before_anything_runs(fake, monkeypatch, capsys, case):
    argv = ["test", "registration_service"]
    if case == "no dev venv":
        monkeypatch.setattr(stack, "DEV_VENV_PYTHON", fake.root / "absent")
        expected = f"Create the dev venv from the repo root ({stack.DEV_VENV_SETUP}"
    elif case == "no service interpreter":
        monkeypatch.setenv("CHAT_APP_PYTHON", str(fake.root / "absent"))
        assert stack.main(["test", "registration_service"]) == stack.EXIT_OK  # only the pipeline starts services
        argv = ["test", "full-chain"]
        expected = "The pipeline's services need it. Check the path in CHAT_APP_PYTHON, or unset it"
    elif case == "no pytest-asyncio":
        fake.configure(packages={"dev": {"pytest": "9.0.3"}, "other": PACKAGES})
        expected = "lacks pytest-asyncio"
        # The other suites need only pytest.
        assert stack.main(["test", "agent_skeleton"]) == stack.EXIT_OK
    elif case == "no pytest-cov":
        fake.configure(packages={"dev": {"pytest": "9.0.3", "pytest-asyncio": "1.4.0"}, "other": PACKAGES})
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
    assert stack.main(["test", "agentnet", "full-chain", "--cov", "--python", str(fake.other), "--json"]) == 0
    unit, pipeline = fake.calls()
    assert unit["args"][-2:] == ["--cov=agentnet", "--cov-report="]
    assert unit["env"]["COVERAGE_FILE"] == str(stack.REPO / ".coverage.agentnet")
    assert not any(arg.startswith("--cov") for arg in pipeline["args"])  # its code runs in subprocesses
    [report] = fake.calls("coverage")
    assert report["args"] == ["-m", "coverage", "report", "--omit=*/tests/*"]
    assert report["env"]["COVERAGE_FILE"] == unit["env"]["COVERAGE_FILE"]
    assert json_out(capsys)["suites"][0]["coverage"] == "80%"


def test_parallel_runs_unit_suites_at_once_then_the_pipeline(fake, capsys):
    fake.configure(sleep={"agentnet/tests": 1.0, "devstack/tests": 1.0})
    assert stack.main(["test", "agentnet", "devstack", "full-chain", "--parallel"]) == stack.EXIT_OK
    calls = {c["args"][2]: c for c in fake.calls()}
    first, second = calls["agentnet/tests"], calls["devstack/tests"]
    assert max(first["started"], second["started"]) < min(first["ended"], second["ended"])
    assert calls["tests/pipeline/test_full_chain.py"]["started"] >= max(first["ended"], second["ended"])
    out = capsys.readouterr().out
    for name in ("agentnet", "devstack"):
        assert f"devstack test: {name} ({fake.dev})\n{name}/tests output\n" in out  # each in one piece


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
