"""Checkpoints + scoped revert on real temp git repos with unrelated dirty user work."""

import subprocess
from pathlib import Path

import pytest

from free_claude_code.workbench.checkpoints import (
    REF_PREFIX,
    Checkpoint,
    CheckpointUnsupportedError,
    changed_files_since,
    create_checkpoint,
    diff_since,
    drop_checkpoint,
    git,
    revert_task,
)


def make_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "core.autocrlf", "false")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    if files:
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
    return root


def read(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")


def test_revert_restores_only_task_files_and_keeps_user_work(tmp_path: Path):
    repo = make_repo(
        tmp_path,
        {"a.py": "a = 1\n", "b.py": "b = 1\n", "u.py": "u = 1\n", "s.py": "s = 1\n"},
    )
    # unrelated user state before the task: dirty tracked file, staged edit, untracked file
    (repo / "u.py").write_text("u = 'user edit'\n", encoding="utf-8")
    (repo / "s.py").write_text("s = 'staged'\n", encoding="utf-8")
    git(repo, "add", "s.py")
    (repo / "notes.txt").write_text("user notes\n", encoding="utf-8")
    index_before = git(repo, "diff", "--cached")
    status_before = git(repo, "status", "--porcelain")

    cp = create_checkpoint(repo)
    assert cp.supported and cp.head and cp.commit
    assert git(repo, "rev-parse", REF_PREFIX + cp.id).strip() == cp.commit
    assert git(repo, "stash", "list") == ""
    assert git(repo, "status", "--porcelain") == status_before
    assert git(repo, "diff", "--cached") == index_before

    # the task: modify a.py, create new files, delete b.py, edit the staged file too
    (repo / "a.py").write_text("a = 2\n", encoding="utf-8")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "new.py").write_text("n = 1\n", encoding="utf-8")
    (repo / "b.py").unlink()
    (repo / "s.py").write_text("s = 'task'\n", encoding="utf-8")

    assert sorted(changed_files_since(cp)) == ["a.py", "b.py", "pkg/new.py", "s.py"]
    diff = diff_since(cp)
    assert {f.path: f.status for f in diff.files} == {
        "a.py": "M",
        "b.py": "D",
        "pkg/new.py": "A",
        "s.py": "M",
    }
    assert "+a = 2" in diff.text and "a.py" in diff.stat

    reverted = revert_task(cp)
    assert sorted(reverted) == ["a.py", "b.py", "pkg/new.py", "s.py"]
    assert read(repo, "a.py") == "a = 1\n"
    assert read(repo, "b.py") == "b = 1\n"
    assert not (repo / "pkg" / "new.py").exists()
    assert read(repo, "s.py") == "s = 'staged'\n"  # back to the user's pre-task content
    assert read(repo, "u.py") == "u = 'user edit'\n"
    assert read(repo, "notes.txt") == "user notes\n"
    assert git(repo, "diff", "--cached") == index_before
    assert changed_files_since(cp) == []


def test_revert_subset_ignores_unchanged_and_outside_paths(tmp_path: Path):
    repo = make_repo(tmp_path / "r", {"a.py": "a\n", "b.py": "b\n"})
    (tmp_path / "outside.txt").write_text("keep\n", encoding="utf-8")
    cp = create_checkpoint(repo)
    (repo / "a.py").write_text("A\n", encoding="utf-8")
    (repo / "b.py").write_text("B\n", encoding="utf-8")
    assert revert_task(cp, ["b.py", "missing.py", "../outside.txt", "a.py\\..\\x"]) == [
        "b.py"
    ]
    assert read(repo, "a.py") == "A\n" and read(repo, "b.py") == "b\n"
    assert (tmp_path / "outside.txt").exists()


def test_untracked_user_file_present_at_checkpoint_is_not_deleted(tmp_path: Path):
    repo = make_repo(tmp_path, {"a.py": "a\n"})
    (repo / "draft.md").write_text("draft v1\n", encoding="utf-8")
    cp = create_checkpoint(repo)
    (repo / "draft.md").write_text("task overwrote\n", encoding="utf-8")
    assert revert_task(cp) == ["draft.md"]
    assert read(repo, "draft.md") == "draft v1\n"


def test_ignored_files_are_not_tracked_by_checkpoints(tmp_path: Path):
    repo = make_repo(tmp_path, {".gitignore": "build/\n", "a.py": "a\n"})
    cp = create_checkpoint(repo)
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("x", encoding="utf-8")
    assert changed_files_since(cp) == []


def test_repo_without_commits_and_subdirectory_cwd(tmp_path: Path):
    repo = make_repo(tmp_path, {})
    (repo / "sub").mkdir()
    (repo / "sub" / "x.txt").write_text("x\n", encoding="utf-8")
    cp = create_checkpoint(repo / "sub")
    assert cp.supported and cp.head is None
    (repo / "sub" / "y.txt").write_text("y\n", encoding="utf-8")
    assert changed_files_since(cp) == ["sub/y.txt"]
    assert revert_task(cp) == ["sub/y.txt"]
    assert not (repo / "sub" / "y.txt").exists() and (repo / "sub" / "x.txt").exists()


def test_json_roundtrip_and_drop(tmp_path: Path):
    repo = make_repo(tmp_path, {"a.py": "a\n"})
    cp = create_checkpoint(repo, "abc123")
    assert Checkpoint.from_json(cp.to_json()) == cp
    drop_checkpoint(cp)
    with pytest.raises(subprocess.CalledProcessError):
        git(repo, "rev-parse", "--verify", REF_PREFIX + "abc123")


def test_non_git_directory_is_unsupported(tmp_path: Path):
    cp = create_checkpoint(tmp_path)
    assert not cp.supported and cp.root is None
    with pytest.raises(CheckpointUnsupportedError):
        revert_task(cp)
    with pytest.raises(CheckpointUnsupportedError):
        changed_files_since(cp)
    assert not create_checkpoint(tmp_path / "missing").supported
