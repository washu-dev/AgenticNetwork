"""Tests for the agent generator (answers -> card/prompt -> emitted agent folder).

Stdlib-only; emits into a temp dir, no a2a-sdk/openai/network. Run:
    python -m pytest agent_skeleton/tests/test_generator.py -q
    python -m agent_skeleton.tests.test_generator
"""
from __future__ import annotations

import json
import py_compile
import tempfile
from pathlib import Path

from agent_skeleton.generator.card_builder import build_card
from agent_skeleton.generator.models import AgentAnswers, SkillAnswer, slugify
from agent_skeleton.generator.oasf import parse_tag, resolve
from agent_skeleton.generator.prompt_builder import build_system_prompt
from agent_skeleton.generator.repo_emitter import emit_agent

# A tiny stand-in taxonomy so the test doesn't depend on registration_service.
TAXONOMY = [
    {"name": "QA", "id": 10302, "key": "nlp/information_retrieval_and_synthesis/question_answering"},
]


def _answers() -> AgentAnswers:
    return AgentAnswers(
        name="Weather Bridge Agent",
        description="Wraps an external weather API.",
        endpoint_url="https://api.example.com/forecast",
        protocol="http",
        auth_env="WEATHER_API_TOKEN",
        input_description="a city name",
        output_description="a forecast object",
        example_request="forecast for Paris",
        example_response='{"temp_c": 18}',
        port=19111,
        skills=[
            SkillAnswer(
                name="Get Forecast",
                description="Return the forecast for a place.",
                examples=["What's the weather in Tokyo?"],
                oasf_key="nlp/information_retrieval_and_synthesis/question_answering",
            )
        ],
    )


def test_oasf_resolve_and_tag_roundtrip():
    skill = resolve("nlp/information_retrieval_and_synthesis/question_answering", TAXONOMY)
    assert skill.id == 10302
    parsed = parse_tag(skill.as_tag())
    assert parsed == skill
    assert parse_tag("disaster/triage") is None  # non-oasf tag ignored


def test_build_card_is_routable_and_well_formed():
    card = build_card(_answers(), TAXONOMY)
    assert card["name"] == "Weather Bridge Agent"
    assert card["url"] == "http://AGENT_HOST:19111/"
    skill = card["skills"][0]
    assert skill["id"] == "weather-bridge-agent/get-forecast"
    oasf_tags = [t for t in skill["tags"] if t.startswith("oasf:")]
    assert oasf_tags and parse_tag(oasf_tags[0]).id == 10302  # discoverable


def test_colliding_skill_names_get_unique_ids():
    a = _answers()
    a.skills.append(
        SkillAnswer(name="Get  Forecast!", description="dup-ish name", examples=[])
    )
    card = build_card(a, TAXONOMY)
    ids = [s["id"] for s in card["skills"]]
    assert len(set(ids)) == len(ids), ids  # disambiguated, not duplicated
    assert ids == ["weather-bridge-agent/get-forecast", "weather-bridge-agent/get-forecast-2"]


def test_missing_oasf_key_is_rejected():
    a = _answers()
    a.skills[0].oasf_key = None
    try:
        a.validate()
    except ValueError:
        return
    raise AssertionError("expected validate() to reject answers with no OASF routing skill")


def test_system_prompt_carries_io_and_contract():
    p = build_system_prompt(_answers())
    assert "Weather Bridge Agent" in p and "call_endpoint" in p
    assert "a city name" in p and "forecast for Paris" in p   # I/O understanding
    assert "answer" in p and "tools_used" in p                # output contract


def test_emit_agent_writes_a_complete_folder():
    with tempfile.TemporaryDirectory() as tmp:
        dest = emit_agent(_answers(), TAXONOMY, tmp)
        slug = slugify("Weather Bridge Agent")
        for rel in (f"{slug}.card.json", f"{slug}.py", "Dockerfile", "entrypoint.sh",
                    "requirements.txt", "README.md", "wrapper_engine/spec.py",
                    "wrapper_engine/system_tools/call_endpoint.py"):
            assert (dest / rel).exists(), f"missing {rel}"
        # vendored engine must NOT drag generator/ or tests/ along
        assert not (dest / "wrapper_engine" / "generator").exists()
        assert not (dest / "wrapper_engine" / "tests").exists()
        # emitted server compiles
        py_compile.compile(str(dest / f"{slug}.py"), doraise=True)
        # card parses and is routable
        card = json.loads((dest / f"{slug}.card.json").read_text())
        tags = card["skills"][0]["tags"]
        assert any(parse_tag(t) for t in tags)
        # entrypoint is executable
        import os
        assert os.access(dest / "entrypoint.sh", os.X_OK)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")
    print(f"ALL {len(fns)} PASSED")


if __name__ == "__main__":
    _run_all()
