"""The demo agent (agent_skeleton/demo/) and its separation from the runtime.

The demo is safe to edit because nothing the platform ships depends on it: the
runtime never imports it, and it is left out of a regular install, the registration
service image, the copy vendored into built images and the generator's
wrapper_engine/. These tests hold both halves: the demo works, and the runtime works
without it. The runtime's source has no import statement of the demo and no string that
names it or its folder, which a load by name or path would need (a static check, so code
that runs only on a request counts too), and the runtime imports and builds its apps
without the demo (in a fresh interpreter, so modules this test process already imported
can't hide an import).
"""
from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest
from packaging.version import Version

from agent_skeleton import llm_loop
from agent_skeleton.demo import demo_spec
from agent_skeleton.executor import SkeletonAgentExecutor
from agent_skeleton.serve import build_parser, create_app
from agent_skeleton.spec import load_tool_module, tool_loop_spec
from agent_skeleton.tools import validate_tool_registry

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _PACKAGE_DIR.parent
_NOT_RUNTIME = ("demo", "tests")
# A string that names the demo: its module ("agent_skeleton.demo", for importlib or
# runpy) or its folder as a path part ("demo", "demo/tools.py", "agent_skeleton/demo").
_NAMES_THE_DEMO = re.compile(r"agent_skeleton\.demo\b|(?:^|[/\\])demo(?:[/\\]|$)")


def _docstrings_and_ignore_patterns(tree: ast.AST) -> set[int]:
    """ids of the string constants that cannot load anything: docstrings, and the
    arguments of shutil.ignore_patterns (names of files to leave out of a copy)."""
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skipped.add(id(first.value))
        elif isinstance(node, ast.Call):
            func = node.func
            if getattr(func, "attr", getattr(func, "id", None)) == "ignore_patterns":
                skipped.update(id(arg) for arg in node.args)
    return skipped


def _scan_runtime_for_demo_imports() -> tuple[set[str], list[str]]:
    """``(files scanned, file:line of each demo import or name)`` over the runtime:
    every package under agent_skeleton/ except demo/ and tests/. ast.walk reaches
    imports and strings at any depth, so one inside a function or a tool body counts.
    A string that names the demo or its folder outside a docstring counts too, since
    importlib, runpy or spec.load_tool_module would need one to load the demo."""
    scanned: set[str] = set()
    found: list[str] = []
    stack = [_PACKAGE_DIR]
    while stack:
        folder = stack.pop()
        package = ("agent_skeleton", *folder.relative_to(_PACKAGE_DIR).parts)
        stack += [p for p in folder.iterdir()
                  if p.is_dir() and p.name not in _NOT_RUNTIME and (p / "__init__.py").is_file()]
        for path in folder.glob("*.py"):
            scanned.add(path.relative_to(_PACKAGE_DIR).as_posix())
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            skipped = _docstrings_and_ignore_patterns(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if id(node) not in skipped and _NAMES_THE_DEMO.search(node.value):
                        found.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
                    continue
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    parts = [node.module] if node.module else []
                    if node.level:  # relative: resolve against this file's package
                        parts = [*package[: len(package) - node.level + 1], *parts]
                    module = ".".join(parts)
                    # "from . import demo" names the demo as an imported name.
                    modules = [module, *(f"{module}.{a.name}" for a in node.names)]
                else:
                    continue
                if any(m == "agent_skeleton.demo" or m.startswith("agent_skeleton.demo.")
                       for m in modules):
                    found.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
    return scanned, sorted(found)


def _build_tools_problem(setuptools: str | None, wheel: bool) -> str | None:
    """Why ``pip install --no-build-isolation`` can't build the package with this
    environment's build tools, or None. Without isolation pip uses them as they are:
    setuptools must meet pyproject's ``setuptools>=68``, and setuptools older than
    70.1 builds a wheel only with the separate ``wheel`` package installed."""
    if setuptools is None:
        return "setuptools is not installed"
    if Version(setuptools) < Version("68"):
        return f"setuptools {setuptools} is older than the package's build requirement (>=68)"
    if Version(setuptools) < Version("70.1") and not wheel:
        return f"setuptools {setuptools} builds wheels only with 'wheel', which is not installed"
    return None


# Imports every runtime entry point, builds one spec of each kind and serves each
# through create_app, then fails if any demo module was loaded along the way.
_RUNTIME_PROBE = """
import sys

import agent_skeleton
from agent_skeleton import container_runner, handler_executor
from agent_skeleton.serve import create_app
from agent_skeleton.spec import (
    endpoint_wrapper_spec, llm_wrapper_spec, multi_endpoint_wrapper_spec,
    operation_wrapper_spec, tool_loop_spec,
)
from agent_skeleton.system_tools.call_endpoint import EndpointConfig, OperationConfig

endpoint = EndpointConfig(url="http://127.0.0.1:1/x")
echo = {"type": "function", "function": {"name": "echo", "parameters": {
    "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}
specs = [
    llm_wrapper_spec(name="a", system_prompt="p", preset_tools=["calculator", "current_time"]),
    endpoint_wrapper_spec(name="b", endpoint=endpoint),
    multi_endpoint_wrapper_spec(name="c", endpoints=[endpoint]),
    operation_wrapper_spec(name="d", base=endpoint, operations=[OperationConfig(name="get")]),
    tool_loop_spec(name="e", system_prompt="p", tool_schemas=[echo],
                   tool_registry={"echo": lambda *, text: {"echo": text}}),
]
card = container_runner._tool_loop_card("probe", "Probe", "", 9999)
for spec in specs:
    create_app(card, spec=spec)
demo = sorted(m for m in sys.modules if m.startswith("agent_skeleton.demo") or m.endswith("tool_schemas"))
assert not demo, f"the runtime loaded demo modules: {demo}"
print("runtime ok from", agent_skeleton.__file__)
"""


def test_demo_spec_validates_and_dispatches():
    demo = demo_spec()
    demo.validate()
    assert demo.dispatch("word_count", {"text": "a b c"})["word_count"] == 3


def test_demo_check_command_passes():
    out = subprocess.run(
        [sys.executable, "-m", "agent_skeleton.demo", "check"],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "OK: tool schemas and functions are aligned." in out.stdout


def test_demo_tools_load_as_an_uploaded_tools_module():
    # The demo's tools.py is shaped like an upload, so the uploaded-tools path takes it as is.
    schemas, registry = load_tool_module(str(_PACKAGE_DIR / "demo" / "tools.py"))
    spec = tool_loop_spec(name="demo", system_prompt="p", tool_schemas=schemas, tool_registry=registry)
    assert [s["function"]["name"] for s in spec.tool_schemas] == ["word_count", "reverse_text"]
    assert spec.dispatch("reverse_text", {"text": "ab", "uppercase": True})["reversed"] == "BA"


def test_registration_image_leaves_out_the_demo():
    # The registration image copies agent_skeleton/ and installs it editable, which
    # would reach demo/ if it were in the build context.
    ignored = (_REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "agent_skeleton/demo" in ignored


def test_runtime_entry_points_require_a_spec():
    # The runtime has no default agent to fall back on.
    with pytest.raises(TypeError):
        llm_loop.run_agent({"prompt": "hi"})
    with pytest.raises(TypeError):
        SkeletonAgentExecutor()
    with pytest.raises(TypeError):
        create_app(object())
    with pytest.raises(TypeError):
        validate_tool_registry()
    # serve.py serves only a card it is given; the demo's commands live in agent_skeleton.demo.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve-wrapper", "--endpoint-url", "http://127.0.0.1:1/x"])
    for command in ("check", "serve-a2a"):
        with pytest.raises(SystemExit):
            build_parser().parse_args([command])


def test_runtime_source_never_imports_the_demo():
    # Static, so it also covers an import or a load on the request path (inside
    # execute(), a tool or a fallback), which the probes below never run.
    scanned, found = _scan_runtime_for_demo_imports()
    assert {"executor.py", "llm_loop.py", "serve.py", "system_tools/preset_tools.py"} <= scanned
    assert not found, f"runtime code imports or names agent_skeleton.demo: {found}"


@pytest.mark.parametrize(("string", "names_it"), [
    ("agent_skeleton.demo", True),
    ("agent_skeleton.demo.tools", True),
    ("demo", True),
    ("demo/tools.py", True),
    ("/srv/agent_skeleton/demo", True),
    ("demo-user", False),
    ("a demo agent", False),
    ("agent_skeleton.demonstration", False),
])
def test_demo_name_pattern(string, names_it):
    assert bool(_NAMES_THE_DEMO.search(string)) is names_it


@pytest.mark.parametrize(("setuptools", "wheel", "can_build"), [
    (None, True, False),
    ("65.5.0", True, False),
    ("68.0.0", False, False),
    ("70.0.0", False, False),
    ("68.0.0", True, True),
    ("70.1.0", False, True),
])
def test_offline_build_needs_setuptools_70_1_or_wheel(setuptools, wheel, can_build):
    assert (_build_tools_problem(setuptools, wheel) is None) is can_build


def test_runtime_never_loads_the_demo():
    pytest.importorskip("a2a.types")
    out = subprocess.run(
        [sys.executable, "-c", _RUNTIME_PROBE],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "runtime ok" in out.stdout


def test_installed_runtime_runs_without_the_demo(tmp_path):
    # Built images run a non-editable install, which holds only the packages pyproject
    # lists. Install that way (offline) and run the runtime from it, away from the repo.
    pytest.importorskip("a2a.types")
    setuptools = importlib.metadata.version("setuptools") if importlib.util.find_spec("setuptools") else None
    problem = _build_tools_problem(setuptools, importlib.util.find_spec("wheel") is not None)
    if problem:
        pytest.skip(f"{problem}, so the package can't be built offline")
    source = tmp_path / "pkg-source"
    shutil.copytree(_PACKAGE_DIR, source, ignore=shutil.ignore_patterns(
        "tests", "build", "dist", "*.egg-info", "__pycache__",
    ))
    installed = tmp_path / "installed"
    build = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--no-index",
         "--no-build-isolation", "--target", str(installed), str(source)],
        capture_output=True, text=True, timeout=300,
    )
    assert build.returncode == 0, build.stderr
    assert (installed / "agent_skeleton" / "container_runner.py").is_file()
    assert not (installed / "agent_skeleton" / "demo").exists()

    # -S skips site-packages' .pth files, so an editable install of the repo can't
    # supply the package; this environment's site-packages still supply a2a-sdk etc.
    paths = dict.fromkeys([str(installed), sysconfig.get_path("purelib"), sysconfig.get_path("platlib")])
    out = subprocess.run(
        [sys.executable, "-S", "-c", _RUNTIME_PROBE],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": os.pathsep.join(paths)},
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert f"runtime ok from {installed}" in out.stdout
