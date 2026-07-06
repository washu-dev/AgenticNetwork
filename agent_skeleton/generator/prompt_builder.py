"""prompt_builder — AgentAnswers -> the wrapper's system prompt.

Reuses the engine's build_wrapper_prompt so the output contract stays in lockstep
with normalize_result, then enriches it with the endpoint's I/O description and a
worked example — this is what makes the wrapper "understand the I/O" of the
endpoint. The full prompt is materialized as a string and baked into the emitted
agent file, so it is human-readable and editable in place.
"""
from __future__ import annotations

from ..prompt import build_wrapper_prompt
from .models import AgentAnswers


def _io_criteria(answers: AgentAnswers) -> str:
    parts: list[str] = []
    if answers.input_description:
        parts.append(f"INPUT the endpoint expects: {answers.input_description}")
    if answers.output_description:
        parts.append(f"OUTPUT the endpoint returns: {answers.output_description}")
    if answers.example_request:
        parts.append(f"Example request to send: {answers.example_request}")
    if answers.example_response:
        parts.append(f"Example response to expect: {answers.example_response}")
    return " | ".join(parts)


def build_system_prompt(answers: AgentAnswers) -> str:
    """Materialize the full system prompt for the wrapper agent."""

    # A lightweight stand-in for EndpointConfig: build_wrapper_prompt only reads
    # `.protocol`, so we avoid importing the engine's dataclass here.
    class _Ep:
        protocol = answers.protocol

    return build_wrapper_prompt(
        name=answers.name,
        description=answers.description,
        endpoint=_Ep(),
        io_criteria=_io_criteria(answers),
    )
