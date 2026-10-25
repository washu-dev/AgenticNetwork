"""The demo agent (agent_skeleton/demo/) and its separation from the runtime.

The demo is safe to edit because nothing the platform ships depends on it: the
runtime never imports it, and it is left out of the installed package, the copy
vendored into built images and the generator's wrapper_engine/. These tests hold
both halves: the demo works, and the runtime works without it (in a fresh
interpreter, so modules this test process already imported can't hide an import).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from agent_skeleton import llm_loop
from agent_skeleton.demo import demo_spec
from agent_skeleton.serve import build_parser, create_app
from agent_skeleton.spec import load_tool_module, tool_loop_spec
from agent_skeleton.tools import validate_tool_registry

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _PACKAGE_DIR.parent

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


def test_runtime_entry_points_require_a_spec():
    # The runtime has no default agent to fall back on.
    with pytest.raises(TypeError):
        llm_loop.run_agent({"prompt": "hi"})
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
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip("setuptools is not installed, so the package can't be built offline")
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
