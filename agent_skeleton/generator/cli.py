"""CLI for the agent generator.

    # Dry run — print the card that WOULD be generated:
    python -m agent_skeleton.generator --answers answers.json --print-card

    # Emit the agent into the agent-directory repo:
    python -m agent_skeleton.generator --answers answers.json \
        --dest /path/to/agent-directory

`answers.json` mirrors the registration questions, e.g.:
{
  "name": "Weather Bridge Agent",
  "description": "...",
  "endpoint_url": "https://api.example.com/forecast",
  "protocol": "http",
  "auth_env": "WEATHER_API_TOKEN",
  "input_description": "...", "output_description": "...",
  "example_request": "...", "example_response": "...",
  "port": 19110, "model": "openai/gpt-oss-120b",
  "skills": [
    {"name": "Get Forecast", "description": "...", "examples": ["..."],
     "oasf_key": "nlp/information_retrieval_and_synthesis/question_answering"}
  ]
}
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .card_builder import build_card
from .models import AgentAnswers, SkillAnswer
from .oasf import load_taxonomy
from .repo_emitter import emit_agent, emit_summary


def answers_from_dict(data: dict[str, Any]) -> AgentAnswers:
    skills = [SkillAnswer(**s) for s in data.get("skills", [])]
    fields = {k: v for k, v in data.items() if k != "skills"}
    return AgentAnswers(skills=skills, **fields)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate an LLM-wrapper agent from endpoint answers.")
    parser.add_argument("--answers", required=True, type=Path, help="Path to the answers JSON file.")
    parser.add_argument("--dest", type=Path, help="agent-directory repo root to write the agent into.")
    parser.add_argument("--print-card", action="store_true", help="Print the generated card and exit (no write).")
    parser.add_argument("--overwrite", action="store_true", help="Replace the agent dir if it exists.")
    args = parser.parse_args(argv)

    data = json.loads(args.answers.read_text(encoding="utf-8"))
    answers = answers_from_dict(data)
    taxonomy = load_taxonomy()
    if not taxonomy and any(s.oasf_key for s in answers.skills):
        parser.error(
            "OASF taxonomy unavailable (registration_service not importable) but a skill sets "
            "oasf_key. Run from the AgenticNetwork repo root so registration_service.oasf_skills "
            "imports, or remove oasf_key (note: an agent with no OASF routing skill is undiscoverable)."
        )

    if args.print_card:
        print(json.dumps(build_card(answers, taxonomy), indent=2))
        return

    if not args.dest:
        parser.error("--dest is required unless --print-card is given.")

    dest = emit_agent(answers, taxonomy, args.dest, overwrite=args.overwrite)
    summary = emit_summary(dest)
    print(f"Generated agent '{answers.name}' -> {summary['agent_dir']} ({summary['file_count']} files)")
    for f in summary["files"]:
        if "/wrapper_engine/" not in f:
            print(f"  {f}")
    print("  wrapper_engine/  (vendored engine)")


if __name__ == "__main__":
    main()
