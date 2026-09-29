"""Project file and folder listing for the chat UI's pickers."""

import os
import stat
import subprocess
import sys
from collections.abc import Iterator

from free_claude_code.core.json_types import JsonObject

MAX_RESULTS = 50
WALK_FILE_CAP = 20_000
GIT_TIMEOUT_S = 5.0
_SKIPPED_DIRS = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
)


def _git_files(root: str) -> list[str] | None:
    """Tracked plus untracked-but-not-ignored files, or None outside a git work tree."""

    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.decode("utf-8", errors="replace")
    return [path for path in output.split("\0") if path]


def _walk_files(root: str) -> Iterator[str]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in _SKIPPED_DIRS
        )
        rel_dir = os.path.relpath(dirpath, root)
        for name in sorted(filenames):
            if count >= WALK_FILE_CAP:
                return
            count += 1
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            yield rel.replace(os.sep, "/")


def search_project_files(root: str, query: str, limit: int = MAX_RESULTS) -> list[str]:
    """Return up to ``limit`` relative POSIX file paths containing ``query``."""

    needle = query.strip().lower()
    git_files = _git_files(root)
    candidates = _walk_files(root) if git_files is None else git_files
    matches: list[str] = []
    for rel in candidates:
        if needle not in rel.lower():
            continue
        # git lists submodules and nested untracked repos as directory entries.
        if rel.endswith("/") or os.path.isdir(os.path.join(root, rel)):
            continue
        matches.append(rel)
        if len(matches) >= limit:
            break
    return matches


_HIDDEN_ATTRIBUTES = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM


def _is_hidden(entry: os.DirEntry[str]) -> bool:
    if entry.name.startswith("."):
        return True
    # Windows-only attribute; DirEntry.stat() is cached from the scandir call there.
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & _HIDDEN_ATTRIBUTES)


def list_directories(path: str) -> JsonObject:
    """Immediate visible subdirectories of ``path`` for the folder browser.

    Raises ``NotADirectoryError`` when ``path`` is not an existing folder and
    ``PermissionError`` when it cannot be listed.
    """

    root = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    found: list[tuple[str, str]] = []
    with os.scandir(root) as entries:
        for entry in entries:
            try:
                if entry.is_dir() and not _is_hidden(entry):
                    found.append((entry.name, entry.path))
            except OSError:
                continue
    found.sort(key=lambda item: item[0].casefold())
    parent = os.path.dirname(root)
    result: JsonObject = {
        "path": root,
        "parent": None if parent == root else parent,
        "dirs": [{"name": name, "path": full} for name, full in found],
    }
    if sys.platform == "win32":
        result["roots"] = list(os.listdrives())
    return result
