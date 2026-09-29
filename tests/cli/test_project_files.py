"""@-mention file search: git listing, filesystem fallback, filtering, and caps."""

import shutil
import subprocess
from pathlib import Path

import pytest

from free_claude_code.cli.managed import project_files
from free_claude_code.cli.managed.project_files import search_project_files


def _touch(root: Path, *paths: str) -> None:
    for rel in paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")


def test_fallback_walk_skips_hidden_and_vendor_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A bogus GIT_DIR makes `git ls-files` fail even if tmp is inside a repo.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "no-such-git-dir"))
    _touch(
        tmp_path,
        "src/App.py",
        "src/util/helpers.py",
        ".env",
        ".git/config",
        ".hidden/secret.py",
        "node_modules/pkg/index.js",
        ".venv/lib/site.py",
        "venv/lib/site.py",
        "src/__pycache__/app.pyc",
        "dist/out.js",
        "build/out.js",
    )
    assert search_project_files(str(tmp_path), "") == [
        ".env",
        "src/App.py",
        "src/util/helpers.py",
    ]
    assert search_project_files(str(tmp_path), "app") == ["src/App.py"]
    assert search_project_files(str(tmp_path), "UTIL/HELP") == ["src/util/helpers.py"]
    assert search_project_files(str(tmp_path), "src") == [
        "src/App.py",
        "src/util/helpers.py",
    ]  # directories themselves never appear
    assert search_project_files(str(tmp_path), "nothing-matches") == []


def test_result_and_walk_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "no-such-git-dir"))
    _touch(tmp_path, *(f"f{n:03}.txt" for n in range(120)))
    first = search_project_files(str(tmp_path), "")
    assert len(first) == project_files.MAX_RESULTS == 50
    assert first[0] == "f000.txt"
    monkeypatch.setattr(project_files, "WALK_FILE_CAP", 10)
    assert len(search_project_files(str(tmp_path), "")) == 10


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_listing_respects_gitignore_and_excludes_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _touch(
        tmp_path,
        ".gitignore",
        "ignored.log",
        "build/kept.js",
        "src/main.py",
        "nested/inner.txt",
    )
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    # An untracked nested repo is listed by git as a directory entry.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path / "nested", check=True)

    files = search_project_files(str(tmp_path), "")
    assert set(files) == {".gitignore", "build/kept.js", "src/main.py"}
    assert search_project_files(str(tmp_path), "MAIN") == ["src/main.py"]


def test_git_failure_falls_back_to_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(project_files.subprocess, "run", missing_git)
    _touch(tmp_path, "a.txt")
    assert search_project_files(str(tmp_path), "") == ["a.txt"]
