"""Small contract fixes ported from the standalone agent-skeleton repo (2026-10-15).

Tool-schema envelope (e40580d), AgentHandler defaults (6e4d371), AGENT_A2A_PORT
parsing (3e1dc8c) and the a2a-sdk install hint (ce70bdc).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from agent_skeleton import a2a_runtime
from agent_skeleton.base import AgentHandler
from agent_skeleton.config import env_port
from agent_skeleton.tools import validate_tool_registry


def _noop(*, q: str) -> str:
    return q


def _schema(**overrides):
    schema = {
        "type": "function",
        "function": {
            "name": "noop",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        },
    }
    schema.update(overrides)
    return schema


def test_well_formed_schema_passes():
    validate_tool_registry([_schema()], {"noop": _noop})


def test_wrong_function_type_is_reported():
    with pytest.raises(ValueError, match="top-level schema 'type' must be 'function'"):
        validate_tool_registry([_schema(type="nope")], {"noop": _noop})


def test_parameters_without_object_type_are_reported():
    schema = _schema()
    schema["function"]["parameters"]["type"] = "array"
    with pytest.raises(ValueError, match="parameters.type must be 'object'"):
        validate_tool_registry([schema], {"noop": _noop})


def test_missing_envelope_types_only_warn(caplog):
    # vLLM fills these in, so an approved agent that omits them must keep starting.
    schema = _schema()
    del schema["type"]
    del schema["function"]["parameters"]["type"]
    with caplog.at_level("WARNING", logger="agent_skeleton.tools"):
        validate_tool_registry([schema], {"noop": _noop})
    assert "no top-level 'type'" in caplog.text
    assert "parameters have no 'type'" in caplog.text


@pytest.mark.parametrize("params", [None, {}])
def test_no_argument_tool_may_omit_parameters(params):
    fn = {"name": "ping"} if params is None else {"name": "ping", "parameters": params}
    validate_tool_registry([{"type": "function", "function": fn}], {"ping": lambda: "pong"})


class _Echo(AgentHandler):
    async def handle_structured(self, user_input, files=None, context=None):
        return {"answer": f"{user_input}:{files!r}"}


def test_handler_files_default_is_not_a_shared_list():
    for method in (AgentHandler.handle_structured, AgentHandler.handle):
        assert inspect.signature(method).parameters["files"].default is None
    assert asyncio.run(_Echo({}).handle("hi")) == "hi:[]"


def test_non_numeric_port_names_the_variable(monkeypatch):
    monkeypatch.setenv("AGENT_A2A_PORT", "eighty")
    with pytest.raises(ValueError, match="AGENT_A2A_PORT must be an integer, got 'eighty'"):
        env_port()
    monkeypatch.setenv("AGENT_A2A_PORT", "9123")
    assert env_port() == 9123


def test_install_hint_names_the_http_server_extra(monkeypatch):
    monkeypatch.setattr(a2a_runtime, "A2A_IMPORT_ERROR", ImportError("no module named a2a"))
    with pytest.raises(RuntimeError, match=r"a2a-sdk\[http-server\]==0\.3\.2"):
        a2a_runtime.require_a2a()
