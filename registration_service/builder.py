"""Generate the per-agent Docker build context committed to agent-directory.

On approval (cluster path), the registration service commits three files to
agent-directory/registered_agents/{slug}/: handler.py, Dockerfile, {slug}.card.json.
A GitHub Actions workflow there builds and pushes the image.
"""

from __future__ import annotations

from typing import Any

from registration_service.models import CustomConfig


def generate_dockerfile(config: CustomConfig) -> str:
    """Render a Dockerfile for a custom agent image.

    agent_skeleton is vendored into the build context (registered_agents/{slug}/
    agent_skeleton/) by the registration service at commit time, so the image
    installs it from that local copy — no access to the private AgenticNetwork repo
    and no build-time secret is needed. Installing the package pulls its declared
    deps (a2a-sdk, openai, uvicorn). The entrypoint is agent_skeleton.container_runner,
    which serves the handler over A2A; handler.py is COPYed in; runtime config
    arrives via AGENT_CONFIG_JSON (set by ClusterBackend).
    """
    req_line = ""
    if config.requirements:
        pkgs = " ".join(f'"{r}"' for r in config.requirements)
        req_line = f"RUN pip install --no-cache-dir {pkgs}\n"

    return (
        f"FROM python:{config.python_version}-slim-bookworm\n"
        "\n"
        "WORKDIR /app\n"
        "\n"
        "# Agent framework — vendored into the build context (no external repo access)\n"
        "COPY agent_skeleton/ agent_skeleton/\n"
        "RUN pip install --no-cache-dir ./agent_skeleton\n"
        "\n"
        "# Agent-specific requirements\n"
        f"{req_line}"
        "\n"
        "COPY handler.py handler.py\n"
        "ENV PYTHONUNBUFFERED=1\n"
        'ENTRYPOINT ["python", "-m", "agent_skeleton.container_runner"]\n'
    )


def generate_card_json(agent: dict[str, Any]) -> dict[str, Any]:
    """Build an Agent Card dict for the committed {slug}.card.json (informational).

    The authoritative ADS record is published by the supervisor at spawn time; this
    file documents the agent in the repo and is handy for manual inspection.
    """
    skills = agent.get("oasf_skills") or []
    description = agent.get("description", "")
    port = agent.get("port")
    return {
        "name": agent.get("name", ""),
        "description": description,
        "url": f"http://AGENT_HOST:{port}/" if port else "http://AGENT_HOST/",
        "version": "1.0.0",
        "capabilities": {"streaming": False},
        "skills": [
            {
                "id": s.get("key") or s.get("name", ""),
                "name": (s.get("name", "").split("/")[-1]),
                "description": description,
                "tags": [],
            }
            for s in skills
        ],
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }
