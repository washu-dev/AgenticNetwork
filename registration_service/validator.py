"""AST-based validation of uploaded custom handler files.

Pure static analysis — no uploaded code is ever imported or executed here. This
is one of the two primary safeguards for custom handlers (the other being the
admin approval gate). Container isolation handles the rest in production.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

BLOCKED_IMPORTS = {"subprocess", "socket", "shutil"}
BLOCKED_CALLS = {"eval", "exec", "os.system", "os.popen"}


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]


def validate_handler(source: str, class_name: str) -> ValidationResult:
    errors: list[str] = []

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return ValidationResult(ok=False, errors=[f"Syntax error at line {e.lineno}: {e.msg}"])

    # Check blocked imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BLOCKED_IMPORTS:
                    errors.append(f"Line {node.lineno}: disallowed import '{alias.name}'")
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BLOCKED_IMPORTS:
                errors.append(f"Line {node.lineno}: disallowed import from '{node.module}'")

    # Check blocked calls (eval/exec, os.system/os.popen)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_CALLS:
                errors.append(f"Line {node.lineno}: disallowed call '{func.id}'")
            elif isinstance(func, ast.Attribute):
                dotted = _dotted_name(func)
                if dotted in BLOCKED_CALLS:
                    errors.append(f"Line {node.lineno}: disallowed call '{dotted}'")

    # Check class exists
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    if class_name not in classes:
        errors.append(f"Class '{class_name}' not found in file")
        return ValidationResult(ok=len(errors) == 0, errors=errors)

    cls = classes[class_name]

    # Check AgentHandler in bases (AST-level, no import needed)
    base_names = []
    for base in cls.bases:
        if isinstance(base, ast.Name):
            base_names.append(base.id)
        elif isinstance(base, ast.Attribute):
            base_names.append(base.attr)
    if "AgentHandler" not in base_names:
        errors.append(f"Class '{class_name}' must extend AgentHandler")

    # Check handle_structured is defined (sync or async)
    methods = {
        n.name
        for n in ast.walk(cls)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "handle_structured" not in methods:
        errors.append(f"Class '{class_name}' must define handle_structured()")

    return ValidationResult(ok=len(errors) == 0, errors=errors)


def _dotted_name(node: ast.Attribute) -> str:
    parts = [node.attr]
    cur: ast.expr = node.value
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))
