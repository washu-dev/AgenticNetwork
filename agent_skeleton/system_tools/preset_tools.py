"""Preset-tool catalog — built-in tools a creator can arm an LLM wrapper with.

The "LLM wrapper" agent type used to be system-prompt-only. This catalog lets a
creator select tools *by name* (e.g. ``["calculator", "current_time"]``); the
spec builder (``spec.llm_wrapper_spec``) resolves those names into the
``(schema, fn)`` pairs the frozen engine already knows how to run.

Design rules (so this stays safe and driftless):
  * Each tool is a ``(schema, fn)`` pair in the SAME shape as ``tool_schemas`` /
    ``tools`` — the ``validate_tool_registry`` alignment check must pass.
  * Tool bodies take keyword args named exactly like their schema properties and
    return a JSON-able dict.
  * The starter set is deliberately PURE and dependency-free (no network, no
    secrets, no filesystem) so it needs no security review. Tools that touch the
    network or credentials are intentionally deferred to decision D2 in
    ``docs/WRAPPER_PRESETS_AND_ENDPOINT_MANAGER_PLAN.md``.
"""
from __future__ import annotations

import ast
import copy
import operator
from datetime import datetime, timezone
from typing import Any, Callable

ToolFn = Callable[..., dict[str, Any]]


# --- calculator: safe arithmetic (no eval, numbers only) ------------------

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numeric constants are allowed")
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) >= 1000:
            raise ValueError("exponent too large")
        return _BIN_OPS[type(node.op)](left, right)
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def calculator(*, expression: str) -> dict[str, Any]:
    """Evaluate a basic arithmetic expression safely (numbers + - * / % ** // only)."""
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree)
    except Exception as exc:  # malformed / disallowed input -> tool error, not a crash
        return {"ok": False, "error": f"could not evaluate {expression!r}: {exc}"}
    return {"ok": True, "expression": expression, "result": result}


_CALCULATOR_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": (
            "Evaluate a basic arithmetic expression. Supports + - * / % ** // and "
            "parentheses over numbers only — no variables or function calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "The arithmetic expression, e.g. '2 * (3 + 4)'.",
                },
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    },
}


# --- current_time: UTC clock (no args) ------------------------------------

def current_time() -> dict[str, Any]:
    """Return the current UTC date/time as ISO-8601 and a Unix timestamp."""
    now = datetime.now(timezone.utc)
    return {"ok": True, "utc_iso": now.isoformat(), "unix": now.timestamp()}


_CURRENT_TIME_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "current_time",
        "description": "Return the current date and time in UTC (ISO-8601 string + Unix timestamp).",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


# --- Catalog + resolver ---------------------------------------------------

# name -> (schema, fn). Add new entries here; each must pass validate_tool_registry.
PRESET_TOOLS: dict[str, tuple[dict[str, Any], ToolFn]] = {
    "calculator": (_CALCULATOR_SCHEMA, calculator),
    "current_time": (_CURRENT_TIME_SCHEMA, current_time),
}


def available_preset_tools() -> list[str]:
    """The names a creator may choose from."""
    return sorted(PRESET_TOOLS)


def resolve_preset_tools(
    names: list[str],
) -> tuple[list[dict[str, Any]], dict[str, ToolFn]]:
    """Turn a list of catalog names into (schemas, registry) for an AgentSpec.

    Schemas are deep-copied so callers can't mutate the shared catalog. Raises
    ValueError listing any unknown names (fail fast at spec-build time)."""
    schemas: list[dict[str, Any]] = []
    registry: dict[str, ToolFn] = {}
    unknown: list[str] = []
    for name in names:
        entry = PRESET_TOOLS.get(name)
        if entry is None:
            unknown.append(name)
            continue
        schema, fn = entry
        schemas.append(copy.deepcopy(schema))
        registry[name] = fn
    if unknown:
        raise ValueError(
            f"Unknown preset tool(s): {unknown}. Available: {available_preset_tools()}"
        )
    return schemas, registry
