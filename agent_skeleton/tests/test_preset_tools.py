"""Tests for the preset-tool catalog + llm_wrapper_spec.

Stdlib-only; no a2a-sdk / openai / network (the engine loop itself isn't run
here — that needs an LLM. We test the catalog, resolution, alignment, and spec
assembly, which is all the new logic). Run:
    python -m pytest agent_skeleton/tests/test_preset_tools.py -q
    python -m agent_skeleton.tests.test_preset_tools
"""
from __future__ import annotations

from agent_skeleton.spec import llm_wrapper_spec
from agent_skeleton.system_tools.preset_tools import (
    PRESET_TOOLS,
    available_preset_tools,
    calculator,
    current_time,
    resolve_preset_tools,
)
from agent_skeleton.tools import validate_tool_registry


def test_calculator_basic() -> None:
    assert calculator(expression="2 * (3 + 4)") == {
        "ok": True,
        "expression": "2 * (3 + 4)",
        "result": 14,
    }


def test_calculator_rejects_non_numeric_and_names() -> None:
    # No variables / function calls / attribute access.
    for expr in ["__import__('os')", "foo + 1", "os.system('x')", "1 +"]:
        out = calculator(expression=expr)
        assert out["ok"] is False, expr
        assert "error" in out


def test_calculator_guards_huge_exponent() -> None:
    out = calculator(expression="9 ** 100000")
    assert out["ok"] is False and "exponent" in out["error"]
    # Boundary: magnitude 1000 is rejected (guard is >= 1000).
    assert calculator(expression="2 ** 1000")["ok"] is False
    assert calculator(expression="2 ** 999")["ok"] is True


def test_current_time_shape() -> None:
    out = current_time()
    assert out["ok"] is True
    assert isinstance(out["utc_iso"], str) and out["utc_iso"].endswith("+00:00")
    assert isinstance(out["unix"], float)


def test_catalog_entries_are_self_aligned() -> None:
    # Every catalog tool's schema must align with its function signature, so it
    # passes the same validate_tool_registry check the engine runs at startup.
    for name, (schema, fn) in PRESET_TOOLS.items():
        validate_tool_registry([schema], {name: fn})


def test_resolve_preset_tools_ok() -> None:
    schemas, registry = resolve_preset_tools(["calculator", "current_time"])
    assert {s["function"]["name"] for s in schemas} == {"calculator", "current_time"}
    assert set(registry) == {"calculator", "current_time"}
    # Returned schemas are copies (mutating one must not touch the catalog).
    schemas[0]["function"]["name"] = "mutated"
    assert PRESET_TOOLS["calculator"][0]["function"]["name"] == "calculator"


def test_resolve_preset_tools_unknown_raises() -> None:
    try:
        resolve_preset_tools(["calculator", "does_not_exist"])
    except ValueError as exc:
        assert "does_not_exist" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown preset tool")


def test_llm_wrapper_spec_no_tools_is_prompt_only() -> None:
    spec = llm_wrapper_spec(name="Bare", system_prompt="You are helpful.")
    assert spec.tool_schemas == []
    assert spec.tool_registry == {}
    # Prompt is used verbatim when there are no tools.
    assert spec.system_prompt == "You are helpful."
    spec.validate()  # must not raise


def test_llm_wrapper_spec_with_tools_validates_and_dispatches() -> None:
    spec = llm_wrapper_spec(
        name="Mathy",
        system_prompt="You are a math helper.",
        preset_tools=["calculator"],
    )
    names = {s["function"]["name"] for s in spec.tool_schemas}
    assert names == {"calculator"}
    # With tools, a tool-awareness nudge is appended (creator prompt preserved).
    assert spec.system_prompt.startswith("You are a math helper.")
    assert "tools available" in spec.system_prompt
    # The spec's dispatch actually calls the tool body.
    assert spec.dispatch("calculator", {"expression": "6 * 7"})["result"] == 42
    spec.validate()  # alignment holds


def test_llm_wrapper_spec_unknown_tool_fails_fast() -> None:
    try:
        llm_wrapper_spec(name="X", system_prompt="hi", preset_tools=["nope"])
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown preset tool")


def test_available_preset_tools_lists_catalog() -> None:
    assert available_preset_tools() == sorted(PRESET_TOOLS)


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\nall {len(fns)} checks passed")


if __name__ == "__main__":
    _run()
