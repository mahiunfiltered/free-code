"""Git helpers: clean-base check and one worktree + branch per mutating node."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from free_claude_code.workbench.ignore import split_ignored

WORKTREE_DIR = ".fcc-worktrees"
BRANCH_PREFIX = "fcc"


class GitError(RuntimeError):
    """A git command failed, or the repository is not in a usable state."""


def git(cwd: Path | str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.rstrip()


def require_clean_repo(repo: Path) -> str:
    """Return HEAD's sha; refuse non-repos, empty repos, and dirty trees."""

    if git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise GitError(f"Not a git repository: {repo}")
    exclude_worktree_dir(repo)
    head = git(repo, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
    if not head:
        raise GitError("The repository has no commits yet; commit a base first.")
    dirty = git(repo, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        files = [line[3:] for line in dirty.splitlines()]
        raise GitError(
            "Commit or stash your changes before a parallel run. Dirty files: "
            + ", ".join(files)
        )
    return head


def current_branch(repo: Path) -> str:
    branch = git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    if not branch:
        raise GitError("HEAD is detached; check out a branch first.")
    return branch


def exclude_worktree_dir(repo: Path) -> None:
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = repo / exclude
    entry = f"/{WORKTREE_DIR}/"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if entry not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join([*lines, entry]) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


def create_worktree(repo: Path, task_id: str, node_id: str, start: str) -> Worktree:
    path = repo / WORKTREE_DIR / task_id / node_id
    branch = f"{BRANCH_PREFIX}/{task_id}/{node_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(path), start)
    return Worktree(path, branch)


def remove_worktree(repo: Path, path: Path, branch: str | None) -> None:
    """Best-effort: remove the worktree dir, then its branch."""

    git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "worktree", "prune", check=False)
    if branch:
        git(repo, "branch", "-D", branch, check=False)


def commit_all(path: Path, message: str, ignore: list[str] | None = None) -> bool:
    """Stage everything but ``ignore`` globs and commit; False when nothing to commit.

    Ignored (tool-state) paths stay as they are in the worktree, uncommitted.
    """

    git(path, "add", "-A")
    if ignore:
        staged = git(path, "diff", "--cached", "--name-only", "--no-renames", "-z")
        _, ignored = split_ignored([p for p in staged.split("\0") if p], ignore)
        for i in range(0, len(ignored), 100):  # keep the command line short
            git(path, "reset", "-q", "--", *ignored[i : i + 100])
    if not git(path, "diff", "--cached", "--name-only"):
        return False
    git(path, "commit", "-q", "-m", message)
    return True


def uncommitted(path: Path) -> list[str]:
    """Paths that differ from HEAD (modified, deleted or untracked, not gitignored)."""

    raw = git(path, "ls-files", "-z", "-o", "-m", "-d", "--exclude-standard")
    return sorted({p for p in raw.split("\0") if p})


def reset_working_branch(repo: Path, branch: str, base_commit: str) -> list[str]:
    """Drop a parallel run's integration commits from its working branch.

    ``git reset --keep`` moves ``branch`` back to ``base_commit`` and refuses to
    touch uncommitted edits in the affected files. Returns the files the commits changed.
    """

    if current_branch(repo) != branch:
        raise GitError(f"Check out {branch} to revert this parallel task.")
    changed = git(repo, "diff", "--name-only", "--no-renames", base_commit, "HEAD")
    git(repo, "reset", "-q", "--keep", base_commit)
    return [path for path in changed.splitlines() if path]
