"""AST-based validation of the uploaded custom handler's entry file.

Pure static analysis — no uploaded code is ever imported or executed here.

Two kinds of finding (D1):
  * **errors** — structural problems that make the upload unusable (won't parse,
    the declared class is missing / doesn't subclass AgentHandler / lacks
    handle_structured). These block the upload (422).
  * **warnings** — advisory notes surfaced to the human approver (use of
    subprocess/socket/shutil, eval/exec/os.system/os.popen). These are NOT a
    security boundary and do NOT block: the real boundary is admin approval +
    container isolation. Scanning the whole uploaded tree is deliberately not
    done — it false-rejects legitimate deps (e.g. an OCR path shelling out).

The validator only ever inspects the entry file (the adapter that bridges the
user's code into AgentHandler), never the rest of the uploaded project.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

BLOCKED_IMPORTS = {"subprocess", "socket", "shutil"}
BLOCKED_CALLS = {"eval", "exec", "os.system", "os.popen"}


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str] = field(default_factory=list)


def validate_handler(source: str, class_name: str) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return ValidationResult(ok=False, errors=[f"Syntax error at line {e.lineno}: {e.msg}"])

    # Advisory: flag blocked imports for the approver (does not block — D1).
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BLOCKED_IMPORTS:
                    warnings.append(f"Line {node.lineno}: uses import '{alias.name}'")
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BLOCKED_IMPORTS:
                warnings.append(f"Line {node.lineno}: uses import from '{node.module}'")

    # Advisory: flag blocked calls (eval/exec, os.system/os.popen) for the approver.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_CALLS:
                warnings.append(f"Line {node.lineno}: calls '{func.id}'")
            elif isinstance(func, ast.Attribute):
                dotted = _dotted_name(func)
                if dotted in BLOCKED_CALLS:
                    warnings.append(f"Line {node.lineno}: calls '{dotted}'")

    # Structural (blocking): the declared class must exist...
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    if class_name not in classes:
        errors.append(f"Class '{class_name}' not found in file")
        return ValidationResult(ok=False, errors=errors, warnings=warnings)

    cls = classes[class_name]

    # ...subclass AgentHandler (AST-level, no import needed)...
    base_names = []
    for base in cls.bases:
        if isinstance(base, ast.Name):
            base_names.append(base.id)
        elif isinstance(base, ast.Attribute):
            base_names.append(base.attr)
    if "AgentHandler" not in base_names:
        errors.append(f"Class '{class_name}' must extend AgentHandler")

    # ...and define handle_structured (sync or async).
    methods = {
        n.name
        for n in ast.walk(cls)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "handle_structured" not in methods:
        errors.append(f"Class '{class_name}' must define handle_structured()")

    return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)


def _dotted_name(node: ast.Attribute) -> str:
    parts = [node.attr]
    cur: ast.expr = node.value
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))
