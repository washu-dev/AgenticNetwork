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
import shutil
import stat
import zipfile
from pathlib import Path

# Top-level names reserved for platform-injected build-context files. A user
# archive that carries these at its root would collide with what the builder /
# supervisor write alongside the project tree (see builder.generate_dockerfile
# and supervisor._commit_to_github).
RESERVED_ROOT_NAMES = {"agent_skeleton", "Dockerfile"}

# Directories skipped when re-zipping / listing a stored tree (build artifacts,
# never part of the uploaded project).
IGNORED_DIRS = {".venv", "__pycache__", ".git"}


def _max_uncompressed() -> int:
    return int(os.getenv("MAX_ARCHIVE_UNCOMPRESSED", str(25 * 1024 * 1024)))  # 25 MB


def _max_files() -> int:
    return int(os.getenv("MAX_ARCHIVE_FILES", "2000"))


class ArchiveError(ValueError):
    """Raised when an uploaded archive fails a safety check (→ HTTP 422)."""


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
