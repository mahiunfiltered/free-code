"""Task checkpoints and scoped revert for git projects (M0005 doc 10 §6-8).

Public API:
    create_checkpoint(cwd, checkpoint_id=None) -> Checkpoint   # .supported False outside git
    diff_since(checkpoint) -> TreeDiff                         # files + unified diff + stat
    changed_files_since(checkpoint) -> list[str]
    revert_task(checkpoint, files=None) -> list[str]
    drop_checkpoint(checkpoint) -> None
    Checkpoint.to_json() / Checkpoint.from_json(obj)

A checkpoint snapshots the whole working tree (tracked edits, staged edits and untracked
non-ignored files) into a commit built through a temporary index: the user's index, stash
list and branches are never touched. The commit is pinned under refs/fcc/checkpoints/<id>
so it is not garbage collected. (`git stash create` was not used because it cannot capture
untracked files, which would make pre-existing user files look task-created.)

revert_task restores only files that changed since the checkpoint, to their checkpoint
content (deleting files the task created); unrelated user changes stay untouched.
Non-git projects get an unsupported checkpoint and the UI should hide revert.
"""

import os
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from free_claude_code.core.json_types import JsonObject

REF_PREFIX = "refs/fcc/checkpoints/"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "fcc",
    "GIT_AUTHOR_EMAIL": "fcc@localhost",
    "GIT_COMMITTER_NAME": "fcc",
    "GIT_COMMITTER_EMAIL": "fcc@localhost",
}


class CheckpointUnsupportedError(RuntimeError):
    """Raised for checkpoint operations on a non-git project."""


@dataclass
class Checkpoint:
    id: str
    cwd: str
    root: str | None  # git toplevel; None when unsupported
    head: str | None  # HEAD at checkpoint time (None in a repo without commits)
    commit: str | None  # snapshot commit of the full working tree
    created_at: str

    @property
    def supported(self) -> bool:
        return self.commit is not None

    def to_json(self) -> JsonObject:
        return cast(JsonObject, asdict(self))

    @classmethod
    def from_json(cls, obj: JsonObject) -> Checkpoint:
        def opt(key: str) -> str | None:
            value = obj.get(key)
            return value if isinstance(value, str) else None

        return cls(
            id=str(obj["id"]),
            cwd=str(obj["cwd"]),
            root=opt("root"),
            head=opt("head"),
            commit=opt("commit"),
            created_at=str(obj.get("created_at", "")),
        )


@dataclass
class ChangedFile:
    path: str  # relative to the git root, forward slashes
    status: str  # "A" added, "M" modified, "D" deleted, "T" type change


@dataclass
class TreeDiff:
    files: list[ChangedFile]
    text: str
    stat: str
    tree: str  # snapshot tree of the current working tree


def git(root: str | Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=off",
            "--literal-pathspecs",
            "-C",
            str(root),
            *args,
        ],
        capture_output=True,
        env={**os.environ, **env} if env else None,
        check=False,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            ["git", *args],
            proc.stdout,
            proc.stderr.decode("utf-8", "replace"),
        )
    return proc.stdout.decode("utf-8", "replace")


def _snapshot_tree(root: str) -> str:
    """Write the working tree (incl. untracked, non-ignored) as a tree via a temp index."""

    fd, index = tempfile.mkstemp(prefix="fcc-index-")
    os.close(fd)
    try:
        real_index = Path(root, git(root, "rev-parse", "--git-path", "index").strip())
        if real_index.is_file():
            Path(index).write_bytes(real_index.read_bytes())  # reuse stat cache
        else:
            Path(index).unlink()
        env = {"GIT_INDEX_FILE": index}
        # ponytail: hashes every non-ignored file once; fine for source repos, slow for
        # repos with huge un-ignored build outputs.
        git(root, "add", "-A", env=env)
        return git(root, "write-tree", env=env).strip()
    finally:
        Path(index).unlink(missing_ok=True)


def _require(cp: Checkpoint) -> tuple[str, str]:
    if cp.root is None or cp.commit is None:
        raise CheckpointUnsupportedError(
            f"checkpoint {cp.id} is not in a git repository"
        )
    return cp.root, cp.commit


def create_checkpoint(cwd: str | Path, checkpoint_id: str | None = None) -> Checkpoint:
    """Snapshot the working tree; returns an unsupported checkpoint outside git."""

    cp_id = checkpoint_id or uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    try:
        root = git(cwd, "rev-parse", "--show-toplevel").strip()
    except subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError:
        return Checkpoint(cp_id, str(cwd), None, None, None, now)
    try:
        head: str | None = git(root, "rev-parse", "-q", "--verify", "HEAD").strip()
    except subprocess.CalledProcessError:
        head = None
    tree = _snapshot_tree(root)
    parents = ["-p", head] if head else []
    commit = git(
        root,
        "commit-tree",
        tree,
        *parents,
        "-m",
        f"fcc checkpoint {cp_id}",
        env=_IDENTITY,
    ).strip()
    git(root, "update-ref", REF_PREFIX + cp_id, commit)
    return Checkpoint(cp_id, str(cwd), root, head, commit, now)


def diff_since(cp: Checkpoint) -> TreeDiff:
    root, commit = _require(cp)
    tree = _snapshot_tree(root)
    raw = git(root, "diff", "--no-renames", "--name-status", "-z", commit, tree).split(
        "\0"
    )
    files = [
        ChangedFile(path=raw[i + 1], status=raw[i][:1])
        for i in range(0, len(raw) - 1, 2)
    ]
    text = git(
        root, "diff", "--no-renames", "--no-color", "--no-ext-diff", commit, tree
    )
    stat = git(root, "diff", "--no-renames", "--stat", commit, tree)
    return TreeDiff(files=files, text=text, stat=stat, tree=tree)


def changed_files_since(cp: Checkpoint) -> list[str]:
    return [f.path for f in diff_since(cp).files]


def revert_task(cp: Checkpoint, files: list[str] | None = None) -> list[str]:
    """Restore task-changed files to checkpoint content; returns the reverted paths.

    Paths not changed since the checkpoint (or outside the repo) are ignored, so
    unrelated user work can never be discarded.
    """

    root, commit = _require(cp)
    changed = changed_files_since(cp)
    wanted = changed if files is None else [f.replace("\\", "/") for f in files]
    targets = [f for f in wanted if f in changed]
    if not targets:
        return []
    existed = set(
        filter(
            None,
            git(
                root, "ls-tree", "-r", "-z", "--name-only", commit, "--", *targets
            ).split("\0"),
        )
    )
    restore = [f for f in targets if f in existed]
    if restore:
        git(root, "restore", f"--source={commit}", "--worktree", "--", *restore)
    root_path = Path(root).resolve()
    for rel in targets:
        if rel in existed:
            continue
        path = (root_path / rel).resolve()
        if path.is_relative_to(root_path) and path.is_file():
            path.unlink()
    return targets


def drop_checkpoint(cp: Checkpoint) -> None:
    """Remove the pinning ref (the snapshot becomes collectable)."""

    root, _ = _require(cp)
    git(root, "update-ref", "-d", REF_PREFIX + cp.id)
