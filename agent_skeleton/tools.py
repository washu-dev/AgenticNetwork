"""ZONE 4 — Tool dispatch + tool bodies.  ★ WRITE THE BODIES ★ (dispatch is copy)

- Tool BODIES (word_count, reverse_text): your real Python — replace them with
  your agent's actual capabilities. Each takes keyword args named exactly like
  its schema `properties` and returns a JSON-able dict.
- TOOL_REGISTRY: the {name: function} map. This REPLACES the hand-written
  if/elif chain in disaster_response_agent.py:679-718. Adding a tool = write a
  function + add one registry entry + add one schema in tool_schemas.py. The LLM
  loop dispatches over this registry via ``AgentSpec.dispatch`` (see spec.py).
- validate_tool_registry(): the startup alignment check the disaster agent
  never had. It fails fast if a schema and its function disagree, instead of
  failing silently at runtime as `{"ok": False, "error": ...}`.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable

from .tool_schemas import TOOL_SCHEMAS


# --- Tool bodies  ★ WRITE THESE ★ ----------------------------------------

def word_count(*, text: str) -> dict[str, Any]:
    return {"ok": True, "word_count": len(text.split()), "char_count": len(text)}


def reverse_text(*, text: str, uppercase: bool = False) -> dict[str, Any]:
    reversed_text = text[::-1]
    if uppercase:
        reversed_text = reversed_text.upper()
    return {"ok": True, "reversed": reversed_text}


# --- Registry (one entry per tool)  ★ EDIT ★ -----------------------------

TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "word_count": word_count,
    "reverse_text": reverse_text,
}


# --- The alignment check (the gap disaster never closed) -----------------

def validate_tool_registry(
    schemas: list[dict[str, Any]] | None = None,
    registry: dict[str, Callable[..., dict[str, Any]]] | None = None,
) -> None:
    """Fail fast if schemas and functions disagree. Called by serve.create_app.

    For every tool it checks that:
      * the schema `name` has a function (and every function has a schema);
      * every schema property is a keyword parameter of the function;
      * every OPTIONAL property's parameter carries a default (so the model may
        omit it without a TypeError at call time);
      * the function has no required parameter that the schema does not declare.
    A function may declare **kwargs to opt out of the strict parameter checks.

    Raises ValueError listing ALL problems; returns None when everything aligns.
    """
    schemas = TOOL_SCHEMAS if schemas is None else schemas
    registry = TOOL_REGISTRY if registry is None else registry

    problems: list[str] = []
    schema_names: list[str] = []

    for schema in schemas:
        fn_spec = schema.get("function") or {}
        name = str(fn_spec.get("name") or "")
        if not name:
            problems.append("a schema entry is missing function.name")
            continue
        schema_names.append(name)

        params = fn_spec.get("parameters") or {}
        props = set((params.get("properties") or {}).keys())
        required = set(params.get("required") or [])

        fn = registry.get(name)
        if fn is None:
            problems.append(f"[{name}] schema has no function in TOOL_REGISTRY")
            continue

        sig = inspect.signature(fn)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            continue  # function opts out of strict checks via **kwargs

        fn_params = {
            n: p
            for n, p in sig.parameters.items()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        fn_required = {n for n, p in fn_params.items() if p.default is inspect.Parameter.empty}

        # schema -> function
        for prop in sorted(props):
            if prop not in fn_params:
                problems.append(f"[{name}] schema property '{prop}' is not a parameter of {fn.__name__}()")
        for prop in sorted(props - required):
            if prop in fn_params and fn_params[prop].default is inspect.Parameter.empty:
                problems.append(f"[{name}] optional property '{prop}' must have a default in {fn.__name__}()")
        # function -> schema
        for pname in sorted(fn_params):
            if pname not in props:
                problems.append(f"[{name}] {fn.__name__}() parameter '{pname}' is not declared in the schema")
        for pname in sorted(fn_required):
            if pname not in required:
                problems.append(f"[{name}] {fn.__name__}() requires '{pname}' but the schema does not mark it required")

    for name in registry:
        if name not in schema_names:
            problems.append(f"[{name}] function in TOOL_REGISTRY has no schema in TOOL_SCHEMAS")

    if problems:
        raise ValueError("Tool schema/function alignment failed:\n  - " + "\n  - ".join(problems))
