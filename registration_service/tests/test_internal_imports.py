"""Every import of this repo's own code under registration_service/, agent_skeleton/ and agentnet/ resolves.

An import that runs only inside a function (runner.py picks its handler module that
way) or a try block fails only when that code runs, so a stale path after a rename
can pass every other test. This reads the source instead of running it: it finds
every import statement, at the top level or nested anywhere, keeps the ones that
name this repo's packages (or are relative), and checks that each one imports and
that each name in ``from X import a`` exists. Third-party imports are ignored.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Folders to scan. A folder that does not exist (yet, or any more) is skipped.
_SCAN_ROOTS = ("registration_service", "agent_skeleton", "agentnet")
# This repo's top-level packages. Imports of anything else count as third-party.
# ads_utils and agent_directory_service were merged into agentnet, and planner_agent
# became chat_app; they stay listed so a leftover import of any of them fails here
# instead of passing as third-party.
_OWN_PACKAGES = frozenset({
    "planner_agent", "chat_app", "registration_service", "agent_skeleton",
    "agentnet", "ads_utils", "agent_directory_service", "devstack",
})


def _package_dirs(root: Path):
    """``root`` and every folder below it that is a package (has ``__init__.py``),
    so virtualenvs, caches and uploaded agent code are never scanned."""
    stack = [root]
    while stack:
        folder = stack.pop()
        yield folder
        stack.extend(
            sorted(p for p in folder.iterdir() if p.is_dir() and (p / "__init__.py").is_file())
        )


def _own_imports(root: Path) -> list[tuple[str, str, tuple[str, ...]]]:
    """``(file:line, module, names)`` for each import of this repo's code under root."""
    found = []
    for folder in _package_dirs(root):
        package = folder.relative_to(_REPO_ROOT).parts
        for path in sorted(folder.glob("*.py")):
            where = path.relative_to(_REPO_ROOT)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
                if isinstance(node, ast.Import):
                    found += [(f"{where}:{node.lineno}", a.name, ()) for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    parts = [node.module] if node.module else []
                    if node.level:  # relative: resolve against this file's package
                        parts = [*package[: len(package) - node.level + 1], *parts]
                    names = tuple(a.name for a in node.names if a.name != "*")
                    found.append((f"{where}:{node.lineno}", ".".join(parts), names))
    return [f for f in found if f[1].split(".")[0] in _OWN_PACKAGES]


def _problem(module: str, names: tuple[str, ...]) -> str | None:
    try:
        imported = importlib.import_module(module)
    except Exception as exc:
        return f"import {module}: {type(exc).__name__}: {exc}"
    for name in names:
        if hasattr(imported, name):
            continue
        try:  # not an attribute: it must be a submodule
            importlib.import_module(f"{module}.{name}")
        except Exception as exc:
            return f"from {module} import {name}: {type(exc).__name__}: {exc}"
    return None


@pytest.mark.parametrize("root", [r for r in _SCAN_ROOTS if (_REPO_ROOT / r).is_dir()])
def test_internal_imports_resolve(root: str) -> None:
    imports = _own_imports(_REPO_ROOT / root)
    assert imports, f"found no imports of this repo's packages under {root}/"
    problems = sorted(
        {f"{where}: {p}" for where, module, names in imports if (p := _problem(module, names))}
    )
    assert not problems, "unresolved imports:\n" + "\n".join(problems)
