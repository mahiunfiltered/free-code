"""Verified-only solution memory: promotion rules, secret rejection, staleness, search."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from free_claude_code.core.storage import Store
from free_claude_code.workbench.memory import (
    MemoryEntry,
    MemoryRejectedError,
    SolutionMemory,
    hash_files,
    render_for_prompt,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "api.py").write_text("def route(): ...\n", encoding="utf-8")
    return root


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[SolutionMemory]:
    store = Store(tmp_path / "fcc.db")
    yield SolutionMemory(store, clock=lambda: 100.0)
    store.close()


def _add(
    memory: SolutionMemory,
    project: Path,
    *,
    title: str = "Add FastAPI route with pydantic model",
    pattern: str = "Declare the request model, then register the route on the router.",
    applicability: str = "FastAPI api endpoints",
    source_task_id: str = "task-1",
    source_hashes: dict[str, str] | None = None,
    confidence: float = 0.8,
) -> MemoryEntry:
    return memory.add_candidate(
        project=str(project),
        title=title,
        pattern=pattern,
        applicability=applicability,
        source_task_id=source_task_id,
        source_hashes=hash_files(project, ["src/api.py"])
        if source_hashes is None
        else source_hashes,
        confidence=confidence,
    )


def test_candidates_are_not_searchable_until_verified_promotion(memory, project):
    entry = _add(memory, project)
    assert entry.status == "candidate" and entry.created_at == 100.0
    assert memory.search(str(project), "fastapi route") == []
    with pytest.raises(MemoryRejectedError):
        memory.promote("task-1", verified=False)
    assert memory.get(entry.id).status == "candidate"
    assert memory.promote("task-1", verified=True) == 1
    assert memory.promote("task-1", verified=True) == 0  # already promoted
    assert memory.promote("other-task", verified=True) == 0
    found = memory.search(str(project), "FastAPI route")
    assert [e.id for e in found] == [entry.id] and found[0].status == "promoted"
    with pytest.raises(KeyError):
        memory.get(9999)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pattern", "export NVIDIA_NIM_API_KEY=nvapi-abcdefgh12345678"),
        ("title", "use sk-proj-0123456789abcdef"),
        ("applicability", "aws AKIAABCDEFGHIJKLMNOP"),
        ("pattern", "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."),
        ("pattern", "-----BEGIN OPENSSH PRIVATE KEY-----"),
    ],
)
def test_secrets_are_rejected(memory, project, field, value):
    with pytest.raises(MemoryRejectedError, match="secret"):
        texts = {"title": "t", "pattern": "p", "applicability": "a", field: value}
        _add(
            memory,
            project,
            title=texts["title"],
            pattern=texts["pattern"],
            applicability=texts["applicability"],
        )
    assert memory.search(str(project), "route") == []


def test_non_secret_lookalikes_and_bad_input(memory, project):
    ok = _add(memory, project, pattern="mask-12345678 and task-abcdefgh are ids")
    assert ok.status == "candidate"
    with pytest.raises(MemoryRejectedError):
        _add(memory, project, confidence=1.5)
    with pytest.raises(MemoryRejectedError):
        _add(memory, project, title="  ")


def test_source_changes_mark_entries_stale_and_block_promotion(memory, project):
    kept = _add(memory, project, source_task_id="t-keep", source_hashes={})
    entry = _add(memory, project)
    memory.promote("task-1", verified=True)
    assert memory.mark_stale(str(project)) == 0
    (project / "src" / "api.py").write_text("def route(x): ...\n", encoding="utf-8")
    assert memory.mark_stale(str(project)) == 1
    assert memory.get(entry.id).status == "stale"
    assert memory.get(kept.id).status == "candidate"
    assert memory.mark_stale(str(project)) == 0
    assert memory.search(str(project), "fastapi route") == []
    assert memory.promote("task-1", verified=True) == 0  # stale never comes back

    gone = _add(memory, project, source_task_id="t2")
    (project / "src" / "api.py").unlink()
    assert memory.mark_stale(str(project)) == 1
    assert memory.get(gone.id).status == "stale"


def test_search_ranks_by_overlap_scoped_by_project_and_limited(
    memory, project, tmp_path
):
    weak = _add(
        memory,
        project,
        title="Logging setup",
        pattern="configure route logging",
        applicability="loguru",
        source_task_id="a",
    )
    strong = _add(memory, project, source_task_id="b")
    other = _add(memory, tmp_path / "elsewhere", source_task_id="c")
    for task in ("a", "b", "c"):
        memory.promote(task, verified=True)
    ranked = memory.search(str(project), "fastapi route")
    assert [e.id for e in ranked] == [strong.id, weak.id]
    assert other.id not in [e.id for e in ranked]
    assert memory.search(str(project), "fastapi route", limit=1) == [ranked[0]]
    assert memory.search(str(project), "") == []
    assert memory.search(str(project), "zebra") == []


def test_render_for_prompt_is_compact(memory, project):
    assert render_for_prompt([]) == ""
    entry = _add(memory, project, pattern="line one\n\n   line two " + "x" * 2000)
    block = render_for_prompt([entry])
    lines = block.splitlines()
    assert lines[0].startswith("Verified solution patterns")
    assert lines[1].startswith("- Add FastAPI route with pydantic model (applies when:")
    assert "line one line two" in lines[1] and len(lines[1]) < 800
    assert len(lines) == 3


def test_migration_is_idempotent_and_shares_store(tmp_path: Path, project):
    store = Store(tmp_path / "shared.db")
    first = SolutionMemory(store)
    _add(first, project)
    second = SolutionMemory(store)
    assert second.promote("task-1", verified=True) == 1
    store.close()
    assert hash_files(project, ["missing.txt"]) == {"missing.txt": ""}
