"""Bring node work back: merge branches, or apply node diffs to a working tree.

``integrate`` merges node branches into the checked-out base branch (Parallel mode).
``apply_to_worktree`` applies each node's diff to the user's working tree with a
per-file 3-way merge, never touching the index or committing (Ultra mode, which
bases worktrees on a checkpoint snapshot so dirty trees work).
Out-of-scope edits are reverted on the node's own branch first (and reported);
conflicts never overwrite anything and are reported. Tool-state paths matching the
shared ignore globs (``workbench.ignore``, as in the verification gate) are never
reverted, applied or reported.
``scan_tree``/``changed_paths`` detect what an in-place node (no git) changed.
The outcome is at most ``ready_for_verification``: only the verification gate
may mark work VERIFIED.
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from free_claude_code.core.json_types import JsonObject
from free_claude_code.workbench.ignore import split_ignored

from .graph import TaskGraph, TaskNode, in_scope
from .worktrees import WORKTREE_DIR, GitError, git

# Folders an in-place scan never descends into (VCS data, caches, dependencies).
_SKIP_DIRS = frozenset(
    {
        ".git",
        WORKTREE_DIR,
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)
type FileStamp = tuple[int, int]  # (mtime_ns, size)


@dataclass
class IntegrationResult:
    merged: list[str] = field(default_factory=list)
    reverted_out_of_scope: dict[str, list[str]] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    final_diff_stat: str = ""

    @property
    def status(self) -> Literal["ready_for_verification", "conflicts"]:
        return "conflicts" if self.conflicts else "ready_for_verification"

    def to_json(self) -> JsonObject:
        return {
            "status": self.status,
            "merged": list(self.merged),
            "reverted_out_of_scope": {
                k: list(v) for k, v in self.reverted_out_of_scope.items()
            },
            "conflicts": {k: list(v) for k, v in self.conflicts.items()},
            "final_diff_stat": self.final_diff_stat,
        }


def enforce_scope(
    worktree: Path, node: TaskNode, ignore: list[str] | None = None
) -> list[str]:
    """Revert files the node changed outside its write scope; returns those paths.

    Paths matching ``ignore`` (tool state) are left alone.
    """

    start = node.start_commit
    if not start:
        return []
    changed = git(worktree, "diff", "--name-only", "--no-renames", start, "HEAD")
    candidates, _ = split_ignored([p for p in changed.splitlines() if p], ignore or [])
    outside = [p for p in candidates if not in_scope(p, node.write_scope)]
    for rel in outside:
        if git(worktree, "ls-tree", "--name-only", start, "--", rel):
            git(worktree, "checkout", start, "--", rel)
        else:
            git(worktree, "rm", "-q", "-f", "--", rel)
    if outside:
        git(
            worktree,
            "commit",
            "-q",
            "-m",
            f"fcc: revert out-of-scope changes of {node.id}: {', '.join(outside)}",
        )
    return outside


def integrate(
    repo: Path, graph: TaskGraph, base_commit: str, ignore: list[str] | None = None
) -> IntegrationResult:
    """Merge completed mutating nodes into the checked-out base branch of ``repo``."""

    result = IntegrationResult()
    for node_id in graph.order():
        node = graph.node(node_id)
        if node.status != "completed" or not node.branch:
            continue
        if node.worktree and Path(node.worktree).is_dir():
            node.reverted_out_of_scope += enforce_scope(
                Path(node.worktree), node, ignore
            )
        if node.reverted_out_of_scope:
            result.reverted_out_of_scope[node.id] = list(node.reverted_out_of_scope)
        try:
            git(
                repo,
                "merge",
                "--no-ff",
                "--no-edit",
                "-m",
                f"fcc: integrate {node.id}",
                node.branch,
            )
            result.merged.append(node.id)
        except GitError as exc:
            files = git(repo, "diff", "--name-only", "--diff-filter=U", check=False)
            git(repo, "merge", "--abort", check=False)
            result.conflicts[node.id] = files.splitlines() or [str(exc)]
    result.final_diff_stat = git(repo, "diff", "--stat", base_commit, "HEAD")
    return result


def apply_to_worktree(
    repo: Path, graph: TaskGraph, ignore: list[str] | None = None
) -> IntegrationResult:
    """Apply completed nodes' changes to ``repo``'s working tree, in dependency order.

    Each node's diff (its ``start_commit`` -> branch tip) is applied file by file:
    a file the user did not touch since the snapshot takes the node's version; a
    file both changed is 3-way merged (``git merge-file``); a merge with conflicts,
    or a binary/deleted file that diverged, is left as is and reported. A node is
    listed in ``merged`` only when all of its files applied.
    """

    result = IntegrationResult()
    applied: list[str] = []
    for node_id in graph.order():
        node = graph.node(node_id)
        if node.status != "completed" or not node.branch or not node.start_commit:
            continue
        if node.reverted_out_of_scope:
            result.reverted_out_of_scope[node.id] = list(node.reverted_out_of_scope)
        diff = ("diff", "--name-only", "--no-renames", "-z")
        raw = git(repo, *diff, node.start_commit, node.branch)
        conflicts: list[str] = []
        paths, _ = split_ignored(list(filter(None, raw.split("\0"))), ignore or [])
        for rel in paths:
            outcome = _apply_file(repo, rel, node.start_commit, node.branch, node.id)
            if outcome == "conflict":
                conflicts.append(rel)
            elif outcome == "applied":
                applied.append(rel)
        if conflicts:
            result.conflicts[node.id] = conflicts
        else:
            result.merged.append(node.id)
    files = list(dict.fromkeys(applied))
    result.final_diff_stat = (
        f"{len(files)} file(s) changed: {', '.join(files)}" if files else ""
    )
    return result


def _apply_file(
    repo: Path, rel: str, base_rev: str, node_rev: str, label: str
) -> Literal["applied", "unchanged", "conflict"]:
    base = _blob(repo, base_rev, rel)
    theirs = _blob(repo, node_rev, rel)
    target = repo / rel
    ours = target.read_bytes() if target.is_file() else None
    if ours == theirs:
        return "unchanged"
    if ours != base:  # the user (or an earlier node) changed it too
        if None in (base, ours, theirs) or any(
            b"\0" in blob for blob in (base, ours, theirs) if blob is not None
        ):
            return "conflict"
        assert base is not None and ours is not None and theirs is not None
        merged = _merge3(repo, ours, base, theirs, label)
        if merged is None:
            return "conflict"
        theirs = merged
    if theirs is None:
        target.unlink(missing_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        # ponytail: content only; file modes (executable bit) are not carried over.
        target.write_bytes(theirs)
    return "applied"


def _blob(repo: Path, rev: str, rel: str) -> bytes | None:
    """File content at ``rev`` as a checkout would write it (eol/filters applied)."""

    proc = subprocess.run(
        ["git", "cat-file", "--filters", f"{rev}:{rel}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def _merge3(
    repo: Path, ours: bytes, base: bytes, theirs: bytes, label: str
) -> bytes | None:
    """Clean 3-way merge of three texts, or None when they conflict."""

    with tempfile.TemporaryDirectory(prefix="fcc-merge-") as tmp:
        paths = []
        for name, content in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = os.path.join(tmp, name)
            Path(path).write_bytes(content)
            paths.append(path)
        labels = ["-L", "yours", "-L", "base", "-L", label]
        proc = subprocess.run(
            ["git", "merge-file", "-p", *labels, *paths],
            cwd=repo,
            capture_output=True,
            check=False,
        )
    return proc.stdout if proc.returncode == 0 else None


def scan_tree(root: Path) -> dict[str, FileStamp]:
    """``{relpath: (mtime_ns, size)}`` of every file under ``root`` (skipping caches)."""

    stamps: dict[str, FileStamp] = {}
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            path = os.path.join(current, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue  # vanished mid-walk
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            stamps[rel] = (stat.st_mtime_ns, stat.st_size)
    return stamps


def changed_paths(
    before: dict[str, FileStamp], after: dict[str, FileStamp]
) -> list[str]:
    """Paths added, removed, or re-stamped between two scans (sorted)."""

    return sorted(
        p for p in before.keys() | after.keys() if before.get(p) != after.get(p)
    )
