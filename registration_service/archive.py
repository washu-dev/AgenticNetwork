"""Safe extraction of an uploaded agent project archive (.zip).

Every rejection raises ArchiveError, which the API surfaces as HTTP 422 with a
clear message. No archive member is written to disk until *every* member has
passed validation (zip-slip, symlinks, size/count caps, reserved names, entry
present), so a malicious or malformed archive can never partially unpack.

The archive root is the project root: the entry file and any importable siblings
(`refbot/`, `pyproject.toml`, …) live at the top level, next to each other.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tomllib
import zipfile
from pathlib import Path

# Top-level names reserved for platform-injected build-context files. A user
# archive that carries these at its root would collide with what the builder /
# supervisor write alongside the project tree (see builder.generate_dockerfile
# and supervisor._commit_to_github).
RESERVED_ROOT_NAMES = {"agent_skeleton", "Dockerfile"}

# The import package the platform vendors and installs on every custom image; the
# container ENTRYPOINT is ``python -m agent_skeleton.container_runner``. An uploaded
# project must not re-declare/install it (see ``_framework_impersonation_reason``).
_FRAMEWORK_PACKAGE = "agent_skeleton"

# Directories skipped when re-zipping / listing a stored tree (build artifacts,
# never part of the uploaded project).
IGNORED_DIRS = {".venv", "__pycache__", ".git"}


def _max_uncompressed() -> int:
    return int(os.getenv("MAX_ARCHIVE_UNCOMPRESSED", str(25 * 1024 * 1024)))  # 25 MB


def _max_files() -> int:
    return int(os.getenv("MAX_ARCHIVE_FILES", "2000"))


class ArchiveError(ValueError):
    """Raised when an uploaded archive fails a safety check (→ HTTP 422)."""


def _normalize_dist_name(name: str) -> str:
    """PEP 503 normalization: distribution names are case-insensitive and treat runs
    of ``-``/``_``/``.`` as equivalent, so ``Agent_Skeleton`` == ``agent-skeleton``."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _framework_impersonation_reason(
    zf: zipfile.ZipFile, manifest: list[str]
) -> str | None:
    """Why the uploaded ``pyproject.toml`` would install itself AS the vendored
    ``agent_skeleton`` framework package (else ``None``).

    The reserved-name check above blocks a literal ``agent_skeleton/`` directory, but
    a project can omit that folder and still claim the package via packaging metadata:
    ``name = "agent-skeleton"``, ``package-dir {agent_skeleton = "."}``, or
    ``packages = ["agent_skeleton", ...]``. On the built image ``pip install /app``
    then *uninstalls* the vendored framework — dropping
    ``agent_skeleton/container_runner.py`` (the ENTRYPOINT) — and the agent fails to
    boot. We reject that at upload instead of shipping a container that dies on start.

    Only ``pyproject.toml`` is inspected (the framework/template's build system); a
    malformed or absent file is skipped (out of scope — the image build surfaces it).
    """
    if "pyproject.toml" not in manifest:
        return None
    try:
        data = tomllib.loads(zf.read("pyproject.toml").decode("utf-8"))
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None

    name = (data.get("project") or {}).get("name")
    if isinstance(name, str) and _normalize_dist_name(name) == "agent-skeleton":
        return 'declares [project] name = "agent-skeleton"'

    setuptools_cfg = (data.get("tool") or {}).get("setuptools") or {}
    package_dir = setuptools_cfg.get("package-dir") or {}
    if isinstance(package_dir, dict) and _FRAMEWORK_PACKAGE in package_dir:
        return (
            f"maps [tool.setuptools.package-dir] {_FRAMEWORK_PACKAGE} = "
            f"{package_dir[_FRAMEWORK_PACKAGE]!r}"
        )

    packages = setuptools_cfg.get("packages")
    if isinstance(packages, list):
        for pkg in packages:
            if isinstance(pkg, str) and (
                pkg == _FRAMEWORK_PACKAGE or pkg.startswith(_FRAMEWORK_PACKAGE + ".")
            ):
                return f"lists {pkg!r} in [tool.setuptools] packages"
    return None


def unpack_archive(raw: bytes, dest: Path, entry_file: str) -> list[str]:
    """Validate a .zip's bytes and extract it into ``dest``.

    ``dest`` is cleared first (re-upload resets the stored tree). Returns the
    sorted manifest of extracted file paths (relative, posix). Raises
    ArchiveError on any violation before touching the filesystem.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ArchiveError("Uploaded file is not a valid .zip archive")

    infos = [i for i in zf.infolist() if not i.is_dir()]

    if len(infos) > _max_files():
        raise ArchiveError(
            f"Archive has too many files ({len(infos)} > {_max_files()})"
        )

    dest_resolved = dest.resolve()
    total = 0
    manifest: list[str] = []
    for info in infos:
        name = info.filename

        # Symlinks are never allowed (they can point outside the tree).
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ArchiveError(f"Archive contains a symlink: {name!r}")

        # Absolute paths and parent traversal (zip-slip).
        if name.startswith("/") or Path(name).is_absolute():
            raise ArchiveError(f"Archive member has an absolute path: {name!r}")
        target = (dest / name).resolve()
        if target != dest_resolved and dest_resolved not in target.parents:
            raise ArchiveError(f"Archive member escapes the target directory: {name!r}")

        # Reserved names at the archive root collide with injected build files.
        parts = Path(name).parts
        top = parts[0] if parts else name
        if top in RESERVED_ROOT_NAMES:
            raise ArchiveError(f"Reserved name at archive root: {top!r}")
        if len(parts) == 1 and name.endswith(".card.json"):
            raise ArchiveError(f"Reserved name at archive root: {name!r}")

        total += info.file_size
        if total > _max_uncompressed():
            raise ArchiveError(
                f"Archive uncompressed size exceeds {_max_uncompressed()} bytes"
            )
        manifest.append(name)

    # The entry file (adapter) must sit at the archive root.
    if entry_file not in manifest:
        raise ArchiveError(
            f"Entry file {entry_file!r} not found at the archive root "
            f"(archive root must be the project root)"
        )

    # Reserved-name analog for packaging metadata: a pyproject that installs itself
    # AS the vendored ``agent_skeleton`` framework would remove the container
    # entrypoint on the built image (see _framework_impersonation_reason). Reject it
    # here with an actionable message rather than shipping an agent that won't boot.
    reason = _framework_impersonation_reason(zf, manifest)
    if reason is not None:
        raise ArchiveError(
            f"Uploaded project must not install itself as the reserved framework "
            f"package {_FRAMEWORK_PACKAGE!r} — it {reason}. The platform vendors "
            f"{_FRAMEWORK_PACKAGE!r} (it provides the container entrypoint), so a "
            f"project re-declaring it removes that entrypoint at build time and the "
            f"agent won't start. Rename your project and package only your own code "
            f'(e.g. name = "myagent", packages = ["my_pkg"]) and drop any package-dir '
            f'mapping {_FRAMEWORK_PACKAGE} = ".". See REGISTERING.md.'
        )

    # All checks passed — clear dest, then extract only the validated members.
    _clear_dir(dest)
    zf.extractall(dest, members=infos)
    return sorted(manifest)


def _clear_dir(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def list_tree(root: Path) -> list[str]:
    """Sorted relative posix paths of the stored project files (build artifacts skipped)."""
    out: list[str] = []
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(root)
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        out.append(rel.as_posix())
    return sorted(out)


def zip_tree(root: Path) -> bytes:
    """Re-zip the stored project tree (build artifacts skipped) for inspection/download."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in list_tree(root):
            zf.write(root / rel, arcname=rel)
    return buf.getvalue()
