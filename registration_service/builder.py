"""Generate the per-agent Docker build context committed to agent-directory.

On approval (cluster path), the registration service commits three files to
agent-directory/registered_agents/{slug}/: handler.py, Dockerfile, {slug}.card.json.
A GitHub Actions workflow there builds and pushes the image.
"""

from __future__ import annotations

from typing import Any

from ads_utils.converter import oasf_skill_tag
from registration_service.models import AnyConfig, upload_spec


def generate_dockerfile(config: AnyConfig) -> str:
    """Render a Dockerfile for an uploaded-code agent image.

    Works for either uploaded-code config — ``custom`` (an ``AgentHandler``
    subclass) or ``llm`` with ``tools.source == "upload"`` (author-supplied tool
    bodies). ``upload_spec`` normalizes both into the same build parameters
    (python_version, system_packages, requirements, install_project, entry_file),
    so this function no longer branches on handler_type. The ENTRYPOINT is the
    shared ``agent_skeleton.container_runner`` either way — it reads the config out
    of AGENT_CONFIG_JSON and picks the right server.

    Layout (build context = registered_agents/{slug}/):
      agent_skeleton/  — vendored framework, installed into the image (no external
                         repo access, no build-time secret; pulls a2a-sdk/openai/uvicorn).
      src/             — the user's uploaded project tree: the entry file at the root
                         plus any importable siblings (refbot/, pyproject.toml, …).

    Ordering (§7.2): apt system_packages first (so a C toolchain via build-essential
    is available to any later pip build — D10), then framework, then the project tree,
    then `pip install /app` (only if the project ships build metadata — D8), then agent
    requirements LAST so they win over project/framework pins. cwd is /app under
    `python -m`, so `import <pkg>` resolves and HANDLER_FILE_PATH points at the entry
    file; runtime config arrives via AGENT_CONFIG_JSON (set by ClusterBackend).

    Note: pip packages needing a C compiler still require the user to add build tools
    (e.g. build-essential) to system_packages — not auto-added here. Package tokens are
    charset-validated at the model layer, so they interpolate into the RUN line safely.
    """
    spec = upload_spec(config)
    if spec is None:
        raise ValueError(
            "generate_dockerfile requires an uploaded-code config "
            "(custom, or llm with tools.source='upload')"
        )

    apt_line = ""
    if spec.system_packages:
        pkgs = " ".join(spec.system_packages)
        apt_line = (
            "# System packages (apt) — arbitrary, flagged for approver review (D10)\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{pkgs} \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
        )

    req_line = ""
    if spec.requirements:
        pkgs = " ".join(f'"{r}"' for r in spec.requirements)
        req_line = (
            "# Agent requirements (additive; installed last so they win) — D8\n"
            f"RUN pip install --no-cache-dir {pkgs}\n\n"
        )

    install_project_line = ""
    if spec.install_project:
        # Gated by install_project; the shell test skips it when no metadata is present.
        install_project_line = (
            "# Install the uploaded project if it ships build metadata (D8)\n"
            "RUN if [ -f /app/pyproject.toml ] || [ -f /app/setup.py ]; then "
            "pip install --no-cache-dir /app; fi\n\n"
        )

    return (
        f"FROM python:{spec.python_version}-slim-bookworm\n"
        "\n"
        "WORKDIR /app\n"
        "\n"
        f"{apt_line}"
        "# Agent framework — vendored into the build context (no external repo access)\n"
        "COPY agent_skeleton/ /opt/agent_skeleton/\n"
        "RUN pip install --no-cache-dir /opt/agent_skeleton\n"
        "\n"
        "# User project tree (entry file + importable siblings) at the workdir root\n"
        "COPY src/ /app/\n"
        "\n"
        f"{install_project_line}"
        f"{req_line}"
        f"ENV PYTHONUNBUFFERED=1 HANDLER_FILE_PATH=/app/{spec.entry_file}\n"
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
        "capabilities": {"streaming": True},
        "skills": [
            {
                "id": s.get("key") or s.get("name", ""),
                "name": (s.get("name", "").split("/")[-1]),
                "description": description,
                "tags": (
                    [oasf_skill_tag(s.get("key") or s.get("name", ""), s["id"])]
                    if s.get("id") is not None
                    else []
                ),
            }
            for s in skills
        ],
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }
