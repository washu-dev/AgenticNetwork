"""`scripts/stack test`: every test suite, each in its own pytest process.

    scripts/stack test                     # all: the unit suites, then the pipeline
    scripts/stack test unit --parallel     # the unit suites at once
    scripts/stack test agentnet            # one suite
    scripts/stack test registration_service -- -k approval -x   # after --: pytest's own args
    scripts/stack test --list              # what would run, with which interpreter

Every suite runs with the dev venv's interpreter (the repo-root .venv, from
requirements-dev.txt), or with ``--python``'s. One process per suite, because the suites
differ in rootdir and asyncio mode (registration_service/pytest.ini applies only when it is
pytest's only path). CI runs the same command with ``--python`` (its job's interpreter) and
``--cov``.

Standard library only. Not named test_*.py, so pytest never collects it.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TextIO

from . import stack

DEFAULT_PORT_OFFSET = 20000  # pipeline ports, unless PIPELINE_PORT_OFFSET says otherwise
UNIT_TIMEOUT = 900.0
PIPELINE_TIMEOUT = 600.0
KILL_GRACE = 60.0  # after SIGTERM on a timeout, before SIGKILL
INTERRUPT_GRACE = 90.0  # after Ctrl-C, for pytest's teardown (the pipeline runs `down`)
TAIL_LINES = 40
# Variables a suite may inherit beyond stack.base_env()'s: the chat app's opt-in Postgres tests.
TEST_PASSTHROUGH_ENV = ("PLANNER_TEST_DATABASE_DSN",)
# The pipeline's `up` starts the services with the interpreters preflight checked.
PIPELINE_PASSTHROUGH_ENV = ("CHAT_APP_PYTHON", "PLANNER_PYTHON", "REGISTRATION_PYTHON")


@dataclass(frozen=True)
class Suite:
    name: str
    path: str  # what pytest collects, relative to the repo root
    cov: str | None = None  # the --cov target; None for the pipeline (its code runs in subprocesses)
    needs: tuple[str, ...] = ()  # packages it needs besides pytest
    # Pipeline suites start a stack on ports PIPELINE_PORT_OFFSET + port_step, in state_dir.
    port_step: int = 0
    state_dir: str | None = None

    @property
    def pipeline(self) -> bool:
        return self.state_dir is not None


SUITES = (
    Suite("chat_app", "chat_app/tests", cov="chat_app"),
    Suite("registration_service", "registration_service/tests", cov="registration_service",
          needs=("pytest-asyncio",)),
    Suite("agent_skeleton", "agent_skeleton/tests", cov="agent_skeleton"),
    Suite("agentnet", "agentnet/tests", cov="agentnet"),
    Suite("devstack", "devstack/tests", cov="devstack"),
    # A stack, ports and state dir per file, run one after the other, so both keep their logs.
    Suite("full-chain", "tests/pipeline/test_full_chain.py", state_dir=".devstack-pipeline"),
    Suite("scenarios", "tests/pipeline/test_scenarios.py", port_step=100, state_dir=".devstack-scenarios"),
)
GROUPS = {
    "unit": tuple(s for s in SUITES if not s.pipeline),
    "pipeline": tuple(s for s in SUITES if s.pipeline),
    "all": SUITES,
}


def select(names: list[str]) -> list[Suite]:
    """The suites ``names`` pick (groups or suite names), in table order."""
    chosen: list[Suite] = []
    for name in names or ["all"]:
        found = GROUPS.get(name) or [s for s in SUITES if s.name == name]
        if not found:
            known = ", ".join([*GROUPS, *(s.name for s in SUITES)])
            raise stack.StackError(f"unknown suite {name!r}; use one of {known}", stack.EXIT_USAGE)
        chosen += [s for s in found if s not in chosen]
    return sorted(chosen, key=SUITES.index)


def port_offset() -> int:
    raw = os.environ.get("PIPELINE_PORT_OFFSET") or str(DEFAULT_PORT_OFFSET)
    try:
        offset = int(raw)
    except ValueError:
        raise stack.StackError(f"PIPELINE_PORT_OFFSET={raw!r} is not a number", stack.EXIT_USAGE) from None
    top = max(s.port_step for s in GROUPS["pipeline"])
    if offset < 0 or stack.AGENT_PORT_END + offset + top > 65535:
        raise stack.StackError(f"PIPELINE_PORT_OFFSET={offset} puts the pipeline's ports out of range", stack.EXIT_USAGE)
    return offset


def preflight(suites: list[Suite], python: str, *, cov: bool, explicit: bool) -> tuple[list[str], list[str]]:
    """(problems, warnings): the interpreters, the packages each suite needs, lsof.
    ``explicit``: ``python`` came from --python rather than being the dev venv."""
    problems: list[str] = []
    warnings: list[str] = []
    # interpreter -> how to fix it if it is missing
    hints = {python: "Check the --python path." if explicit else (
        f"Create the dev venv from the repo root ({stack.DEV_VENV_SETUP}; see docs/DEVSTACK.md) or pass --python.")}
    if any(suite.pipeline for suite in suites):
        for _, py, variable in stack.service_pythons():  # the pipeline's stack runs its services with these
            hints.setdefault(py, f"The pipeline's services need it. {stack.missing_python_hint(variable)}")
        if not shutil.which("lsof"):
            problems.append("lsof not found: the pipeline's `down` needs it to find leftover agents")
    packages: dict[str, dict[str, Any]] = {}
    for py, hint in hints.items():
        if not os.access(py, os.X_OK):
            problems.append(f"interpreter not found: {py}. {hint}")
            continue
        report = stack.venv_report(py)
        if not report.get("ok"):
            problems.append(f"{py} does not run: {report.get('error')}")
            continue
        packages[py] = report["packages"]
    for suite in suites:
        if python in packages:
            wanted = ["pytest", *suite.needs, *(["pytest-cov"] if cov and suite.cov else [])]
            missing = [p for p in wanted if not packages[python].get(p)]
            if missing:
                problems.append(f"{suite.name}: {python} lacks {', '.join(missing)} "
                                f"({python} -m pip install {' '.join(missing)})")
        if suite.name == "devstack" and not shutil.which("lsof"):
            warnings.append("lsof not found: devstack's process tests will be skipped")
    return problems, warnings


def test_env(suite: Suite, offset: int) -> dict[str, str]:
    """stack.base_env() (no stray FAKE_DB=0, database DSN, keys or PYTEST_DISABLE_PLUGIN_AUTOLOAD),
    without the fake dirctl on PATH or PYTHONPATH, as CI runs the suites."""
    env = stack.base_env()
    env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if p != str(stack.DIRCTL_SHIM.parent))
    env.pop("PYTHONPATH", None)
    names = TEST_PASSTHROUGH_ENV + (PIPELINE_PASSTHROUGH_ENV if suite.pipeline else ())
    env.update({name: os.environ[name] for name in names if name in os.environ})
    if suite.pipeline:
        env["PIPELINE_PORT_OFFSET"] = str(offset + suite.port_step)
        env["PIPELINE_STATE_DIR"] = str(stack.REPO / str(suite.state_dir))
    return env


def execute(command: list[str], env: dict[str, str], out: TextIO, timeout: float) -> tuple[int, list[str], bool]:
    """Run ``command``, copying its output to ``out``: (exit code, last lines, timed out).

    On a timeout it gets SIGTERM first (the pipeline turns that into KeyboardInterrupt, so its
    fixture still runs `down`), then SIGKILL after KILL_GRACE seconds."""
    proc = subprocess.Popen(
        command, cwd=stack.REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
    )
    tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)

    def copy() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            out.write(line)
            out.flush()
            tail.append(line)

    reader = threading.Thread(target=copy, daemon=True)
    reader.start()
    timed_out = False
    try:
        try:
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                code = proc.wait(KILL_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
    except KeyboardInterrupt:  # Ctrl-C reached pytest too (same process group): let it tear down
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(INTERRUPT_GRACE)
        raise
    reader.join(5)  # a process the suite left behind may still hold the pipe
    return code, list(tail), timed_out


def summary_line(lines: list[str]) -> str:
    """pytest's final count line, e.g. "256 passed, 12 skipped in 4.31s"."""
    pattern = re.compile(r"\d+ (passed|failed|errors?|skipped|deselected|xfailed|xpassed)|no tests ran")
    return next((line.strip().strip("=").strip() for line in reversed(lines) if pattern.search(line)), "")


def run_suite(suite: Suite, python: str, args: argparse.Namespace, offset: int, out: TextIO) -> dict[str, Any]:
    env = test_env(suite, offset)
    command = [python, "-m", "pytest", suite.path, "-q", "-p", "no:cacheprovider"]
    covered = args.cov and suite.cov is not None
    if covered:
        env["COVERAGE_FILE"] = str(stack.REPO / f".coverage.{suite.name}")  # .gitignore: .coverage.*
        command += [f"--cov={suite.cov}", "--cov-report="]
    command += args.pytest_args
    out.write(f"devstack test: {suite.name} ({python})\n")
    out.flush()
    started = time.monotonic()
    timeout = args.timeout or (PIPELINE_TIMEOUT if suite.pipeline else UNIT_TIMEOUT)
    code, tail, timed_out = execute(command, env, out, timeout)
    result: dict[str, Any] = {
        "suite": suite.name,
        "path": suite.path,
        "python": python,
        "exit_code": code,
        "ok": code == 0 and not timed_out,
        "seconds": round(time.monotonic() - started, 1),
        "timed_out": timed_out,
        "summary": f"stopped after {timeout:g} s" if timed_out else summary_line(tail),
    }
    if suite.pipeline:
        result["logs"] = f"{env['PIPELINE_STATE_DIR']}/logs"
        # Killed or crashed (5: nothing collected, so no stack started): make sure its stack is gone.
        if timed_out or code not in (0, 1, 5):
            stack.main(["down", "--state-dir", env["PIPELINE_STATE_DIR"]])
    if covered and not timed_out and code in (0, 1):
        # Line coverage of the component's own code (its tests left out), as CI prints it.
        report = subprocess.run(
            [python, "-m", "coverage", "report", "--omit=*/tests/*"],
            cwd=stack.REPO, env=env, capture_output=True, text=True, check=False,
        )
        out.write(report.stdout + report.stderr)
        total = re.search(r"^TOTAL\s.*?(\d+(?:\.\d+)?%)\s*$", report.stdout, re.MULTILINE)
        result["coverage"] = total.group(1) if total else None
    return result


def run_all(suites: list[Suite], python: str, args: argparse.Namespace, offset: int,
            out: TextIO) -> list[dict[str, Any]]:
    """The unit suites (at once with --parallel), then the pipeline files, one at a time."""
    unit = [suite for suite in suites if not suite.pipeline]
    sequential = suites
    results: list[dict[str, Any]] = []
    if args.parallel and len(unit) > 1:
        print(f"devstack test: running {', '.join(s.name for s in unit)} at once", file=sys.stderr)
        lock = threading.Lock()

        def buffered(suite: Suite) -> dict[str, Any]:
            buffer = io.StringIO()
            result = run_suite(suite, python, args, offset, buffer)
            with lock:  # each suite's output in one piece, as it finishes
                out.write(buffer.getvalue())
                out.flush()
            return result

        with ThreadPoolExecutor(len(unit)) as pool:
            results += pool.map(buffered, unit)
        sequential = [suite for suite in suites if suite.pipeline]
    for suite in sequential:
        results.append(run_suite(suite, python, args, offset, out))
    return results


def print_summary(results: list[dict[str, Any]], *, as_json: bool) -> None:
    failed = [r["suite"] for r in results if not r["ok"]]
    if as_json:
        print(json.dumps({"ok": not failed, "failed": failed, "suites": results}, indent=2))
        return
    verdict = f"{len(failed)} of {len(results)} failed" if failed else f"all {len(results)} passed"
    print(f"devstack test: {verdict}")
    print(f"  {'SUITE':<24} {'RESULT':<8} {'TIME':>7}  SUMMARY")
    for r in results:
        result = "ok" if r["ok"] else ("TIMEOUT" if r["timed_out"] else "FAILED")
        coverage = f" (coverage {r['coverage']})" if r.get("coverage") else ""
        print(f"  {r['suite']:<24} {result:<8} {r['seconds']:>6.1f}s  {r['summary'] or '-'}{coverage}")
        if not r["ok"] and r.get("logs"):
            print(f"  {'':<24} -> stack logs: {r['logs']}")


def cmd_test(args: argparse.Namespace) -> int:
    if args.timeout is not None and args.timeout <= 0:
        raise stack.StackError("--timeout must be more than 0 seconds", stack.EXIT_USAGE)
    chosen = select(args.suites)
    python = args.python or str(stack.DEV_VENV_PYTHON)
    offset = port_offset() if any(suite.pipeline for suite in chosen) else DEFAULT_PORT_OFFSET
    if args.list:
        rows = [{"suite": s.name, "path": s.path, "python": python, "pipeline": s.pipeline} for s in chosen]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                print(f"{row['suite']:<24} {row['path']:<36} {row['python']}")
        return stack.EXIT_OK
    problems, warnings = preflight(chosen, python, cov=args.cov, explicit=args.python is not None)
    for warning in dict.fromkeys(warnings):
        print(f"devstack test: warning: {warning}", file=sys.stderr)
    if problems:
        raise stack.StackError("preflight failed:\n  - " + "\n  - ".join(problems), stack.EXIT_PREFLIGHT)
    results = run_all(chosen, python, args, offset, sys.stderr if args.json else sys.stdout)
    print_summary(results, as_json=args.json)
    return stack.EXIT_OK if all(r["ok"] for r in results) else stack.EXIT_TESTS


def add_parser(sub: Any) -> None:
    """The `test` subcommand; main() hands it everything after `--` as pytest_args."""
    names = " ".join(s.name for s in SUITES)
    test = sub.add_parser(
        "test", help="Run the test suites with the dev venv, each in its own pytest process.",
        epilog="Everything after -- goes to pytest: scripts/stack test registration_service -- -k approval -x",
    )
    test.add_argument("suites", nargs="*", metavar="SUITE",
                      help=f"all (default), unit, pipeline, or suites: {names}")
    test.add_argument("--parallel", action="store_true",
                      help="Run the unit suites at once; the pipeline still runs after them, one file at a time.")
    test.add_argument("--cov", action="store_true", help="Line coverage per unit suite, as CI prints it (needs pytest-cov).")
    test.add_argument("--python", metavar="PATH", help="Run pytest with this interpreter instead of the dev venv's (CI does).")
    test.add_argument("--timeout", type=float, metavar="SECONDS",
                      help=f"Seconds per suite (default {UNIT_TIMEOUT:g} unit, {PIPELINE_TIMEOUT:g} pipeline).")
    test.add_argument("--list", action="store_true", help="Show what would run, with which interpreter.")
    test.add_argument("--json", action="store_true", help="pytest's output on stderr, a JSON summary on stdout.")
    test.set_defaults(func=cmd_test, pytest_args=[])
