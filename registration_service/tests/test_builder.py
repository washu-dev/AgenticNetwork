"""Tests for Dockerfile generation and agent_skeleton vendoring (Option A)."""

from __future__ import annotations

from registration_service.builder import generate_dockerfile
from registration_service.models import CustomConfig
from registration_service.supervisor import _vendored_skeleton_files


def test_dockerfile_vendors_skeleton_no_external_repo():
    df = generate_dockerfile(
        CustomConfig(class_name="H", python_version="3.12", requirements=["refbot>=0.1"])
    )
    assert "FROM python:3.12-slim-bookworm" in df
    # Vendored install, not a git+ / PyPI fetch of the private repo.
    assert "COPY agent_skeleton/ agent_skeleton/" in df
    assert "pip install --no-cache-dir ./agent_skeleton" in df
    assert "git+" not in df
    assert "AgenticNetwork" not in df
    # Agent requirements + entrypoint present.
    assert '"refbot>=0.1"' in df
    assert 'ENTRYPOINT ["python", "-m", "agent_skeleton.container_runner"]' in df


def test_dockerfile_no_requirements_line_when_empty():
    df = generate_dockerfile(CustomConfig(class_name="H", requirements=[]))
    # No agent-specific pip line, but the framework install is still there.
    assert "pip install --no-cache-dir ./agent_skeleton" in df
    assert df.count("RUN pip install") == 1


def test_vendored_skeleton_files_complete():
    files = _vendored_skeleton_files()
    # The package must be installable + the container entrypoint present.
    for required in (
        "pyproject.toml",
        "__init__.py",
        "base.py",
        "handler_executor.py",
        "a2a_runtime.py",
        "container_runner.py",
    ):
        assert required in files, f"missing vendored file: {required}"
    assert "class AgentHandler" in files["base.py"]
    assert files["pyproject.toml"].strip()
