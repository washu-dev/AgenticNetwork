"""Unit tests for the safe archive-unpack module (registration_service.archive).

Exercises the happy path and every rejection in archive.py directly (no HTTP layer).
"""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

from registration_service.archive import (
    ArchiveError,
    list_tree,
    unpack_archive,
    zip_tree,
)

HANDLER = (
    "from agent_skeleton import AgentHandler\n"
    "class H(AgentHandler):\n"
    "    async def handle_structured(self, u, files=[]):\n"
    "        return {'answer': 'ok'}\n"
)


def _zip(files: dict[str, str], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        if symlink:
            zi = zipfile.ZipInfo(symlink)
            zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(zi, "/etc/passwd")
    return buf.getvalue()


# ── Happy path ───────────────────────────────────────────────────────────────

def test_unpack_stores_tree_and_returns_manifest(tmp_path: Path) -> None:
    dest = tmp_path / "agent"
    manifest = unpack_archive(
        _zip({"handler.py": HANDLER, "refbot/__init__.py": "x = 1", "pyproject.toml": "[project]"}),
        dest,
        "handler.py",
    )
    assert manifest == ["handler.py", "pyproject.toml", "refbot/__init__.py"]
    assert (dest / "handler.py").exists()
    assert (dest / "refbot" / "__init__.py").read_text() == "x = 1"


def test_unpack_clears_existing_tree(tmp_path: Path) -> None:
    dest = tmp_path / "agent"
    dest.mkdir()
    (dest / "stale.py").write_text("old")
    unpack_archive(_zip({"handler.py": HANDLER}), dest, "handler.py")
    assert not (dest / "stale.py").exists()  # re-upload resets the tree


def test_list_tree_and_zip_tree_roundtrip(tmp_path: Path) -> None:
    dest = tmp_path / "agent"
    unpack_archive(_zip({"handler.py": HANDLER, "pkg/mod.py": "y = 2"}), dest, "handler.py")
    # Build artifacts are ignored by list_tree.
    (dest / ".venv").mkdir()
    (dest / ".venv" / "junk").write_text("x")
    assert list_tree(dest) == ["handler.py", "pkg/mod.py"]

    data = zip_tree(dest)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert set(names) == {"handler.py", "pkg/mod.py"}  # .venv excluded from the zip too


# ── Rejections in archive.py ───────────────────────────────────────────────────

def test_reject_not_a_zip(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="not a valid .zip"):
        unpack_archive(b"definitely not a zip", tmp_path / "a", "handler.py")


def test_reject_zip_slip(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="escapes"):
        unpack_archive(_zip({"handler.py": HANDLER, "../evil.py": "x"}), tmp_path / "a", "handler.py")


def test_reject_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="absolute path"):
        unpack_archive(_zip({"handler.py": HANDLER, "/tmp/evil.py": "x"}), tmp_path / "a", "handler.py")


def test_reject_symlink(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="symlink"):
        unpack_archive(_zip({"handler.py": HANDLER}, symlink="link"), tmp_path / "a", "handler.py")


@pytest.mark.parametrize("reserved", ["Dockerfile", "agent_skeleton/__init__.py", "foo.card.json"])
def test_reject_reserved_root_names(tmp_path: Path, reserved: str) -> None:
    with pytest.raises(ArchiveError, match="Reserved name"):
        unpack_archive(_zip({"handler.py": HANDLER, reserved: "x"}), tmp_path / "a", "handler.py")


def test_reject_missing_entry_file(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="not found at the archive root"):
        unpack_archive(_zip({"other.py": HANDLER}), tmp_path / "a", "handler.py")


def test_reject_too_many_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAX_ARCHIVE_FILES", "3")
    files = {"handler.py": HANDLER, "a.py": "1", "b.py": "2", "c.py": "3"}
    with pytest.raises(ArchiveError, match="too many files"):
        unpack_archive(_zip(files), tmp_path / "a", "handler.py")


def test_reject_oversize_uncompressed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAX_ARCHIVE_UNCOMPRESSED", "1024")
    big = "A" * 5000
    with pytest.raises(ArchiveError, match="uncompressed size"):
        unpack_archive(_zip({"handler.py": HANDLER, "big.txt": big}), tmp_path / "a", "handler.py")


def test_rejection_does_not_partially_unpack(tmp_path: Path) -> None:
    dest = tmp_path / "a"
    with pytest.raises(ArchiveError):
        unpack_archive(_zip({"handler.py": HANDLER, "../evil.py": "x"}), dest, "handler.py")
    # A rejected archive writes nothing (validation happens before extraction).
    assert not dest.exists() or not any(dest.iterdir())


# ── Framework-impersonation guard (pyproject that installs itself AS agent_skeleton) ──

_PYPROJECT_IMPERSONATION_NAME = '[project]\nname = "Agent_Skeleton"\nversion = "0.1.0"\n'
_PYPROJECT_PACKAGE_DIR_REMAP = (
    '[project]\nname = "geoagent"\nversion = "0.1.0"\n\n'
    '[tool.setuptools.package-dir]\nagent_skeleton = "."\n'
)
_PYPROJECT_PACKAGES_LIST = (
    '[project]\nname = "geoagent"\nversion = "0.1.0"\n\n'
    '[tool.setuptools]\npackages = ["agent_skeleton.system_tools", "skills"]\n'
)
_PYPROJECT_CLEAN = (
    '[project]\nname = "geoagent"\nversion = "0.1.0"\n\n'
    '[tool.setuptools]\npackages = ["skills"]\n'
)


@pytest.mark.parametrize(
    "pyproject",
    [_PYPROJECT_IMPERSONATION_NAME, _PYPROJECT_PACKAGE_DIR_REMAP, _PYPROJECT_PACKAGES_LIST],
)
def test_reject_pyproject_impersonating_framework(tmp_path: Path, pyproject: str) -> None:
    """An upload whose pyproject would install itself AS the vendored agent_skeleton
    framework (by dist name / package-dir remap / packages list) is rejected — it would
    otherwise uninstall the framework and drop the container_runner entrypoint at build."""
    with pytest.raises(ArchiveError, match="reserved framework package"):
        unpack_archive(
            _zip({"handler.py": HANDLER, "pyproject.toml": pyproject}),
            tmp_path / "a",
            "handler.py",
        )


def test_accept_clean_pyproject_packaging_own_code(tmp_path: Path) -> None:
    """A well-formed custom upload (own dist name, only its own package) is accepted."""
    manifest = unpack_archive(
        _zip(
            {
                "handler.py": HANDLER,
                "pyproject.toml": _PYPROJECT_CLEAN,
                "skills/__init__.py": "",
            }
        ),
        tmp_path / "a",
        "handler.py",
    )
    assert "pyproject.toml" in manifest and "handler.py" in manifest
