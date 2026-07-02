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

    Layout (build context = registered_agents/{slug}/):
      agent_skeleton/  — vendored framework, installed into the image (no external
                         repo access, no build-time secret; pulls a2a-sdk/openai/uvicorn).
      src/             — the user's uploaded project tree: the entry file at the root
                         plus any importable siblings (refbot/, pyproject.toml, …).

    Ordering (§7.2): framework, then the project tree, then `pip install /app` (only
    if the project ships build metadata — D8), then agent requirements LAST so they
    win over project/framework pins. cwd is /app under `python -m`, so `import <pkg>`
    resolves and HANDLER_FILE_PATH points at the entry file; runtime config arrives
    via AGENT_CONFIG_JSON (set by ClusterBackend).

    Note: pip packages needing a C compiler still require the user to add build tools
    via system_packages (P2) — not auto-added here.
    """
    req_line = ""
    if config.requirements:
        pkgs = " ".join(f'"{r}"' for r in config.requirements)
        req_line = (
            "# Agent requirements (additive; installed last so they win) — D8\n"
            f"RUN pip install --no-cache-dir {pkgs}\n\n"
        )

    install_project_line = ""
    if config.install_project:
        # Gated by install_project; the shell test skips it when no metadata is present.
        install_project_line = (
            "# Install the uploaded project if it ships build metadata (D8)\n"
            "RUN if [ -f /app/pyproject.toml ] || [ -f /app/setup.py ]; then "
            "pip install --no-cache-dir /app; fi\n\n"
        )

    return (
        f"FROM python:{config.python_version}-slim-bookworm\n"
        "\n"
        "WORKDIR /app\n"
        "\n"
        "# Agent framework — vendored into the build context (no external repo access)\n"
        "COPY agent_skeleton/ /opt/agent_skeleton/\n"
        "RUN pip install --no-cache-dir /opt/agent_skeleton\n"
        "\n"
        "# User project tree (entry file + importable siblings) at the workdir root\n"
        "COPY src/ /app/\n"
        "\n"
        f"{install_project_line}"
        f"{req_line}"
        f"ENV PYTHONUNBUFFERED=1 HANDLER_FILE_PATH=/app/{config.entry_file}\n"
        # Absolute interpreter path so a leaked host PATH (e.g. conda) can't hijack `python`.
        'ENTRYPOINT ["/usr/local/bin/python", "-m", "agent_skeleton.container_runner"]\n'
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
