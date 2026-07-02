"""Tests for Dockerfile generation and agent_skeleton vendoring (Option A)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from registration_service.builder import generate_dockerfile
from registration_service.models import CustomConfig
from registration_service.supervisor import _vendored_skeleton_files


def test_dockerfile_vendors_skeleton_no_external_repo():
    df = generate_dockerfile(
        CustomConfig(class_name="H", python_version="3.12", requirements=["refbot>=0.1"])
    )
    assert "FROM python:3.12-slim-bookworm" in df
    # Vendored install, not a git+ / PyPI fetch of the private repo.
    assert "COPY agent_skeleton/ /opt/agent_skeleton/" in df
    # User project tree lands at the workdir root.
    assert "COPY src/ /app/" in df
    assert "pip install --no-cache-dir /opt/agent_skeleton" in df
    assert "git+" not in df
    assert "AgenticNetwork" not in df
    # Agent requirements + entrypoint present.
    assert '"refbot>=0.1"' in df
    assert 'ENTRYPOINT ["/usr/local/bin/python", "-m", "agent_skeleton.container_runner"]' in df


def test_dockerfile_no_requirements_line_when_empty():
    df = generate_dockerfile(CustomConfig(class_name="H", requirements=[]))
    # No agent-specific pip line, but the framework install is still there.
    assert "pip install --no-cache-dir /opt/agent_skeleton" in df
    assert df.count("RUN pip install") == 1


def test_dockerfile_src_layout_and_entry_file():
    df = generate_dockerfile(CustomConfig(class_name="H", entry_file="main.py"))
    # Framework installed out of the workdir; user tree at the workdir root.
    assert "COPY agent_skeleton/ /opt/agent_skeleton/" in df
    assert "COPY src/ /app/" in df
    # Entry file wired via env for the container_runner.
    assert "HANDLER_FILE_PATH=/app/main.py" in df
    assert 'ENTRYPOINT ["/usr/local/bin/python", "-m", "agent_skeleton.container_runner"]' in df


def test_dockerfile_install_project_gated():
    # install_project=True (default) → the guarded pip-install-project line is present.
    df_on = generate_dockerfile(CustomConfig(class_name="H"))
    assert "pip install --no-cache-dir /app" in df_on
    assert "[ -f /app/pyproject.toml ]" in df_on
    # install_project=False → no such line.
    df_off = generate_dockerfile(CustomConfig(class_name="H", install_project=False))
    assert "pip install --no-cache-dir /app" not in df_off


def test_dockerfile_requirements_installed_last():
    df = generate_dockerfile(
        CustomConfig(class_name="H", requirements=["numpy"], install_project=True)
    )
    # Ordering (§7.2): COPY src → install project → requirements last (so they win).
    assert df.index("COPY src/ /app/") < df.index("pip install --no-cache-dir /app") \
        < df.index('pip install --no-cache-dir "numpy"')


def test_dockerfile_apt_line_iff_system_packages():
    # No system_packages → no apt line.
    df_off = generate_dockerfile(CustomConfig(class_name="H"))
    assert "apt-get install" not in df_off
    # With system_packages → an apt-get install line listing the packages, run
    # BEFORE the framework install so a C toolchain is available to later pip builds.
    df_on = generate_dockerfile(
        CustomConfig(class_name="H", system_packages=["tesseract-ocr", "build-essential"])
    )
    assert "apt-get update && apt-get install -y --no-install-recommends" in df_on
    assert "tesseract-ocr build-essential" in df_on
    assert "rm -rf /var/lib/apt/lists/*" in df_on
    assert df_on.index("apt-get install") < df_on.index("pip install --no-cache-dir /opt/agent_skeleton")


def test_system_packages_accepts_valid_names_and_version_pins():
    cfg = CustomConfig(
        class_name="H",
        system_packages=["tesseract-ocr", "libpq-dev", "build-essential=12.9", "  ", "gcc:amd64"],
    )
    # Whitespace-only tokens dropped; valid names (incl. version/arch) kept.
    assert cfg.system_packages == ["tesseract-ocr", "libpq-dev", "build-essential=12.9", "gcc:amd64"]


@pytest.mark.parametrize("bad", ["foo && rm -rf /", "foo;bar", "foo bar", "$(id)", "foo\nbar"])
def test_system_packages_rejects_injection(bad):
    # Charset guard blocks shell/Dockerfile breakout before it reaches the RUN line.
    with pytest.raises(ValidationError):
        CustomConfig(class_name="H", system_packages=[bad])


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
