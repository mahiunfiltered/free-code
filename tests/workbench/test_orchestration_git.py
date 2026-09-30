"""Worktrees, integration, and the orchestrator end-to-end on real temp git repos."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from free_claude_code.cli.managed.interactive import (
    InteractiveClaudeSessions,
    PolicyCompiler,
)
from free_claude_code.core.json_types import JsonObject
from free_claude_code.workbench.orchestration.graph import TaskGraph, TaskNode
from free_claude_code.workbench.orchestration.integration import integrate
from free_claude_code.workbench.orchestration.runner import (
    Orchestrator,
    OrchestratorOptions,
)
from free_claude_code.workbench.orchestration.worktrees import (
    WORKTREE_DIR,
    GitError,
    commit_all,
    create_worktree,
    current_branch,
    git,
    remove_worktree,
    require_clean_repo,
    reset_working_branch,
)
from free_claude_code.workbench.service import launch_policy

FAKE = Path(__file__).with_name("fake_claude_worker.py")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for key, value in (
        ("user.email", "t@example.com"),
        ("user.name", "Test"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        git(repo, "config", key, value)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def _branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").split()


# ----- worktrees -------------------------------------------------------------


def test_require_clean_repo_refuses_non_repo_empty_and_dirty(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError, match="Not a git repository"):
        require_clean_repo(plain)
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    with pytest.raises(GitError, match="no commits"):
        require_clean_repo(empty)

    repo = _repo(tmp_path)
    head = require_clean_repo(repo)
    assert head == git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    with pytest.raises(GitError) as err:
        require_clean_repo(repo)
    assert "README.md" in str(err.value) and "new.txt" in str(err.value)


def test_worktree_create_commit_remove_and_exclude(tmp_path: Path):
    repo = _repo(tmp_path)
    base = require_clean_repo(repo)
    require_clean_repo(repo)  # exclude entry is written once
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count(f"/{WORKTREE_DIR}/") == 1

    wt = create_worktree(repo, "t1", "impl", base)
    assert wt.path == repo / WORKTREE_DIR / "t1" / "impl" and wt.path.is_dir()
    assert wt.branch == "fcc/t1/impl" and wt.branch in _branches(repo)
    assert not commit_all(wt.path, "nothing")
    (wt.path / "f.txt").write_text("f\n", encoding="utf-8")
    assert commit_all(wt.path, "add f")
    # The main tree stays clean while node worktrees exist.
    assert require_clean_repo(repo) == base
    assert current_branch(repo) == "main"

    remove_worktree(repo, wt.path, wt.branch)
    assert not wt.path.exists() and wt.branch not in _branches(repo)
    git(repo, "checkout", "-q", "--detach")
    with pytest.raises(GitError, match="detached"):
        current_branch(repo)


# ----- integration -----------------------------------------------------------


def _node_branch(
    repo: Path, base: str, node: TaskNode, files: dict[str, str | None]
) -> TaskNode:
    """Give ``node`` a worktree branch with ``files`` written (None deletes)."""

    wt = create_worktree(repo, "t1", node.id, base)
    node.branch, node.worktree, node.start_commit = wt.branch, str(wt.path), base
    for rel, content in files.items():
        target = wt.path / rel
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    commit_all(wt.path, node.id)
    node.status = "completed"
    return node


def test_integration_merges_in_dependency_order_and_reverts_out_of_scope(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    base = require_clean_repo(repo)
    later = _node_branch(
        repo,
        base,
        TaskNode("later", "b", depends_on=["first"], write_scope=["b/**"]),
        {"b/y.txt": "y\n"},
    )
    first = _node_branch(
        repo,
        base,
        TaskNode("first", "a", write_scope=["a/**"]),
        {
            "a/x.txt": "x\n",
            "README.md": "hacked\n",  # modified outside scope
            "stray.txt": "s\n",  # added outside scope
            "src/app.py": None,  # deleted outside scope
        },
    )
    skipped = TaskNode("ro", "look", status="completed")  # read-only: no branch
    graph = TaskGraph("t1", [later, first, skipped])

    result = integrate(repo, graph, base)

    assert result.merged == ["first", "later"]
    assert result.reverted_out_of_scope == {
        "first": ["README.md", "src/app.py", "stray.txt"]
    }
    assert result.conflicts == {} and result.status == "ready_for_verification"
    assert (repo / "README.md").read_text(encoding="utf-8") == "base\n"
    assert (repo / "src" / "app.py").exists() and not (repo / "stray.txt").exists()
    assert (repo / "a" / "x.txt").exists() and (repo / "b" / "y.txt").exists()
    assert "a/x.txt" in result.final_diff_stat and "README.md" not in (
        result.final_diff_stat
    )
    log = git(repo, "log", "--format=%s", "--merges")
    assert log.splitlines() == ["fcc: integrate later", "fcc: integrate first"]
    assert result.to_json()["status"] == "ready_for_verification"


def test_integration_reports_conflicts_without_overwriting(tmp_path: Path):
    repo = _repo(tmp_path)
    base = require_clean_repo(repo)
    one = _node_branch(
        repo,
        base,
        TaskNode("one", "x", write_scope=["README.md"]),
        {"README.md": "1\n"},
    )
    two = _node_branch(
        repo,
        base,
        TaskNode("two", "x", write_scope=["README.md"]),
        {"README.md": "2\n"},
    )
    result = integrate(repo, TaskGraph("t1", [one, two]), base)
    assert result.merged == ["one"]
    assert result.conflicts == {"two": ["README.md"]}
    assert result.status == "conflicts"
    assert (repo / "README.md").read_text(encoding="utf-8") == "1\n"
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()


# ----- orchestrator end-to-end (fake claude) ---------------------------------


def _sessions(
    tmp_path: Path, policy_compiler: PolicyCompiler | None = None
) -> InteractiveClaudeSessions:
    if os.name == "nt":
        wrapper = tmp_path / "fake_claude.bat"
        wrapper.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "fake_claude"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o755)
    return InteractiveClaudeSessions(
        proxy_target=lambda: ("http://127.0.0.1:1", "token"),
        claude_bin=str(wrapper),
        policy_compiler=policy_compiler,
    )


async def _run(orchestrator: Orchestrator, timeout: float = 60.0) -> list[JsonObject]:
    async def collect() -> list[JsonObject]:
        return [event async for event in orchestrator.run()]

    return await asyncio.wait_for(collect(), timeout)


def _of(events: list[JsonObject], kind: str) -> list[JsonObject]:
    return [e for e in events if e["type"] == kind]


@pytest.mark.asyncio
async def test_parallel_disjoint_nodes_merge_and_readonly_reviewer(tmp_path: Path):
    repo = _repo(tmp_path)
    graph = TaskGraph(
        "t1",
        [
            TaskNode("a", "Add a\nWRITE a/one.txt 1", write_scope=["a/**"]),
            TaskNode("b", "Add b\nWRITE b/two.txt 2", write_scope=["b/**"]),
            TaskNode(
                "review",
                "Review both",
                role="reviewer",
                depends_on=["a", "b"],
                acceptance_criteria=["both files exist"],
            ),
        ],
    )
    sessions = _sessions(tmp_path)
    orchestrator = Orchestrator(
        graph, repo, sessions, OrchestratorOptions(contract_block="MUST: be nice")
    )
    events = await _run(orchestrator)

    types = [e["type"] for e in events]
    assert (
        types[0] == "orchestration_started" and types[-1] == "orchestration_completed"
    )
    started = {e["node_id"]: e for e in _of(events, "node_started")}
    assert started["a"]["permission_mode"] == "acceptEdits"
    assert started["a"]["cwd"] == str(repo / WORKTREE_DIR / "t1" / "a")
    assert started["review"]["permission_mode"] == "plan"
    assert started["review"]["cwd"] == str(repo)
    progress = _of(events, "node_progress")
    assert {e["node_id"] for e in progress} == {"a", "b"}
    assert all(e["tools"] == ["Write"] for e in progress)
    completed = {e["node_id"]: e for e in _of(events, "node_completed")}
    assert completed["a"]["cost_usd"] == 0.01 and completed["a"]["turns"] == 1
    assert completed["a"]["summary"] == "cwd=a wrote 1 files"
    assert types.index("integration_started") < types.index("integration_completed")

    final = events[-1]
    assert final["status"] == "ready_for_verification"
    assert final["kept_worktrees"] == []
    integration = _of(events, "integration_completed")[0]
    assert integration["merged"] == ["a", "b"] and integration["conflicts"] == {}
    assert (repo / "a" / "one.txt").read_text(encoding="utf-8") == "1\n"
    assert (repo / "b" / "two.txt").read_text(encoding="utf-8") == "2\n"
    # main is protected: the run integrated on its own working branch.
    assert events[0]["working_branch"] == final["working_branch"] == "fcc/t1-work"
    assert orchestrator.working_branch == "fcc/t1-work"
    assert current_branch(repo) == "fcc/t1-work"
    assert git(repo, "status", "--porcelain") == ""
    assert _branches(repo) == ["fcc/t1-work", "main"]
    assert git(repo, "rev-parse", "main") == orchestrator.base_commit
    assert not (repo / WORKTREE_DIR / "t1" / "a").exists()
    assert sessions.live_sessions() == []
    assert all(n.status == "completed" and n.session_id for n in graph.nodes)
    prompt = orchestrator._prompt(graph.node("review"))
    assert "MUST: be nice" in prompt and "- a: cwd=a wrote 1 files" in prompt
    assert "read-only" in prompt and "- both files exist" in prompt


@pytest.mark.asyncio
async def test_out_of_scope_write_reverted_and_dependent_sees_dep_work(tmp_path: Path):
    repo = _repo(tmp_path)
    graph = TaskGraph(
        "t2",
        [
            TaskNode(
                "a",
                "WRITE a/ok.txt ok\nWRITE README.md hacked",
                write_scope=["a/**"],
            ),
            TaskNode("b", "WRITE b/next.txt n", depends_on=["a"], write_scope=["b/**"]),
        ],
    )
    events = await _run(Orchestrator(graph, repo, _sessions(tmp_path)))
    completed = {e["node_id"]: e for e in _of(events, "node_completed")}
    assert completed["a"]["reverted_out_of_scope"] == ["README.md"]
    assert completed["b"]["reverted_out_of_scope"] == []
    integration = _of(events, "integration_completed")[0]
    assert integration["reverted_out_of_scope"] == {"a": ["README.md"]}
    assert (repo / "README.md").read_text(encoding="utf-8") == "base\n"
    assert (repo / "a" / "ok.txt").exists() and (repo / "b" / "next.txt").exists()
    # b's worktree started from a's (already scope-cleaned) work.
    b_start = graph.node("b").start_commit
    assert b_start and git(repo, "show", f"{b_start}:a/ok.txt") == "ok"
    assert git(repo, "show", f"{b_start}:README.md") == "base"
    assert events[-1]["status"] == "ready_for_verification"


@pytest.mark.asyncio
async def test_overlapping_scopes_are_serialized(tmp_path: Path):
    repo = _repo(tmp_path)
    graph = TaskGraph(
        "t3",
        [
            TaskNode("x", "WRITE shared/x.txt x", write_scope=["shared/**"]),
            TaskNode("y", "WRITE shared/y.txt y", write_scope=["shared/y.txt"]),
        ],
    )
    events = await _run(Orchestrator(graph, repo, _sessions(tmp_path)))
    order = [
        (e["type"], e["node_id"])
        for e in events
        if e["type"] in ("node_started", "node_completed")
    ]
    assert order == [
        ("node_started", "x"),
        ("node_completed", "x"),
        ("node_started", "y"),
        ("node_completed", "y"),
    ]
    assert (repo / "shared" / "x.txt").exists() and (repo / "shared" / "y.txt").exists()


@pytest.mark.asyncio
async def test_failed_node_blocks_dependents_only_and_keeps_worktree(tmp_path: Path):
    repo = _repo(tmp_path)
    graph = TaskGraph(
        "t4",
        [
            TaskNode("bad", "WRITE bad/f.txt f\nFAIL", write_scope=["bad/**"]),
            TaskNode(
                "after",
                "WRITE after/g.txt g",
                depends_on=["bad"],
                write_scope=["after/**"],
            ),
            TaskNode("good", "WRITE good/h.txt h", write_scope=["good/**"]),
        ],
    )
    events = await _run(Orchestrator(graph, repo, _sessions(tmp_path)))
    failed = _of(events, "node_failed")
    assert [e["node_id"] for e in failed] == ["bad"]
    assert _of(events, "node_blocked") == [{"type": "node_blocked", "node_id": "after"}]
    final = events[-1]
    assert final["status"] == "needs_attention"
    assert final["kept_worktrees"] == [str(repo / WORKTREE_DIR / "t4" / "bad")]
    assert (repo / WORKTREE_DIR / "t4" / "bad").is_dir()
    assert (repo / "good" / "h.txt").exists() and not (repo / "bad").exists()
    assert graph.node("after").status == "blocked"


@pytest.mark.asyncio
async def test_cancellation_interrupts_and_closes_all_sessions(tmp_path: Path):
    repo = _repo(tmp_path)
    graph = TaskGraph(
        "t5",
        [
            TaskNode("h1", "HANG", write_scope=["h1/**"]),
            TaskNode("h2", "HANG"),
            TaskNode("later", "x", depends_on=["h1"]),
        ],
    )
    sessions = _sessions(tmp_path)
    orchestrator = Orchestrator(graph, repo, sessions)
    events: list[JsonObject] = []

    async def consume() -> None:
        async for event in orchestrator.run():
            events.append(event)
            if (
                event["type"] == "node_started"
                and len(_of(events, "node_started")) == 2
            ):
                assert len(sessions.live_sessions()) == 2
                await orchestrator.cancel()
                await orchestrator.cancel()  # idempotent: must not abort cleanup

    await asyncio.wait_for(consume(), 60)
    assert sorted(str(e["node_id"]) for e in _of(events, "node_cancelled")) == [
        "h1",
        "h2",
    ]
    assert events[-1]["type"] == "orchestration_cancelled"
    assert [n.status for n in graph.nodes] == ["cancelled", "cancelled", "cancelled"]
    assert sessions.live_sessions() == []
    assert not (repo / WORKTREE_DIR / "t5" / "h1").exists()
    assert _branches(repo) == ["main"]


@pytest.mark.asyncio
async def test_node_wall_time_budget_fails_the_node(tmp_path: Path):
    repo = _repo(tmp_path)
    graph = TaskGraph("t6", [TaskNode("slow", "HANG")])
    sessions = _sessions(tmp_path)
    options = OrchestratorOptions(node_timeout_s=4.0, interrupt_timeout_s=2.0)
    events = await _run(Orchestrator(graph, repo, sessions, options))
    failed = _of(events, "node_failed")
    assert len(failed) == 1 and "wall time" in str(failed[0]["error"])
    assert events[-1]["status"] == "needs_attention"
    assert _of(events, "integration_started") == []
    assert sessions.live_sessions() == []


@pytest.mark.asyncio
async def test_node_permission_prompt_is_forwarded_and_answer_completes_node(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    graph = TaskGraph("t7", [TaskNode("asker", "ASK Bash")])
    sessions = _sessions(tmp_path)
    events: list[JsonObject] = []

    async def drive() -> None:
        async for event in Orchestrator(graph, repo, sessions).run():
            events.append(event)
            if event["type"] == "node_permission":
                node = sessions.get(str(event["live_id"]))
                assert node is not None  # node sessions share the chat registry
                await node.respond_permission(
                    str(event["request_id"]), {"behavior": "allow", "updatedInput": {}}
                )

    await asyncio.wait_for(drive(), 60)
    [asked] = _of(events, "node_permission")
    [started] = _of(events, "node_started")
    assert asked["node_id"] == "asker" and asked["request_id"] == "perm_Bash"
    assert asked["live_id"] == started["live_id"]
    request = asked["request"]
    assert isinstance(request, dict) and request["tool_name"] == "Bash"
    assert "permission_suggestions" in request
    [resolved] = _of(events, "node_permission_resolved")
    assert resolved == {
        "type": "node_permission_resolved",
        "node_id": "asker",
        "live_id": started["live_id"],
        "request_id": "perm_Bash",
        "behavior": "allow",
    }
    [done] = _of(events, "node_completed")
    assert "Bash=allow" in str(done["summary"])
    assert sessions.live_sessions() == []


@pytest.mark.asyncio
async def test_unanswered_permission_times_out_naming_the_tool(tmp_path: Path):
    repo = _repo(tmp_path)
    graph = TaskGraph("t8", [TaskNode("asker", "ASK Bash")])
    sessions = _sessions(tmp_path)
    options = OrchestratorOptions(permission_timeout_s=1.0, interrupt_timeout_s=2.0)
    events = await _run(Orchestrator(graph, repo, sessions, options))
    [failed] = _of(events, "node_failed")
    assert failed["error"] == "timed out waiting for approval of Bash"
    [resolved] = _of(events, "node_permission_resolved")
    assert resolved["behavior"] is None  # the card closes with the node
    assert sessions.live_sessions() == []


@pytest.mark.asyncio
async def test_node_budget_spent_waiting_on_approval_names_the_tool(tmp_path: Path):
    repo = _repo(tmp_path)
    graph = TaskGraph("t9", [TaskNode("asker", "ASK Edit")])
    sessions = _sessions(tmp_path)
    options = OrchestratorOptions(node_timeout_s=4.0, interrupt_timeout_s=2.0)
    events = await _run(Orchestrator(graph, repo, sessions, options))
    [failed] = _of(events, "node_failed")
    error = str(failed["error"])
    assert error.startswith("timed out waiting for approval of Edit")
    assert "wall time budget" in error


@pytest.mark.asyncio
async def test_run_refuses_dirty_repo_and_invalid_graph(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    graph = TaskGraph("t7", [TaskNode("a", "x")])
    events = await _run(Orchestrator(graph, repo, _sessions(tmp_path)))
    assert events == [
        {
            "type": "orchestration_failed",
            "error": "Commit or stash your changes before a parallel run. "
            "Dirty files: README.md",
        }
    ]
    bad = TaskGraph("t8", [TaskNode("a", "x", depends_on=["a"])])
    events = await _run(Orchestrator(bad, repo, _sessions(tmp_path)))
    assert events[0]["type"] == "orchestration_failed"
    assert "self_dependency" in str(events[0]["error"])


@pytest.mark.asyncio
async def test_unprotected_branch_is_integrated_in_place(tmp_path: Path):
    repo = _repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feature/x")
    graph = TaskGraph("t9", [TaskNode("a", "WRITE a/one.txt 1", write_scope=["a/**"])])
    orchestrator = Orchestrator(graph, repo, _sessions(tmp_path))
    events = await _run(orchestrator)

    assert events[0]["working_branch"] is None and events[-1]["working_branch"] is None
    assert current_branch(repo) == "feature/x"
    assert (repo / "a" / "one.txt").exists()
    assert _branches(repo) == ["feature/x", "main"]


@pytest.mark.parametrize("base", ["master", "release/1.2"])
@pytest.mark.asyncio
async def test_protected_branches_get_a_named_working_branch(tmp_path: Path, base: str):
    repo = _repo(tmp_path)
    git(repo, "checkout", "-q", "-b", base)
    graph = TaskGraph("t10", [TaskNode("a", "WRITE a/one.txt 1", write_scope=["a/**"])])
    options = OrchestratorOptions(working_branch="fcc/task123")
    orchestrator = Orchestrator(graph, repo, _sessions(tmp_path), options)
    events = await _run(orchestrator)

    assert events[-1]["working_branch"] == "fcc/task123"
    assert current_branch(repo) == "fcc/task123"
    assert git(repo, "rev-parse", base) == orchestrator.base_commit


@pytest.mark.asyncio
async def test_working_branch_dropped_when_nothing_is_merged(tmp_path: Path):
    repo = _repo(tmp_path)
    graph = TaskGraph("t11", [TaskNode("bad", "FAIL", write_scope=["bad/**"])])
    orchestrator = Orchestrator(graph, repo, _sessions(tmp_path))
    events = await _run(orchestrator)

    assert events[0]["working_branch"] == "fcc/t11-work"
    assert events[-1]["working_branch"] is None
    assert orchestrator.working_branch is None
    assert current_branch(repo) == "main" and "fcc/t11-work" not in _branches(repo)


@pytest.mark.asyncio
async def test_existing_working_branch_fails_the_run(tmp_path: Path):
    repo = _repo(tmp_path)
    git(repo, "branch", "fcc/t12-work")
    graph = TaskGraph("t12", [TaskNode("a", "WRITE a/one.txt 1")])
    events = await _run(Orchestrator(graph, repo, _sessions(tmp_path)))

    assert [e["type"] for e in events] == ["orchestration_failed"]
    assert current_branch(repo) == "main"


def test_reset_working_branch_drops_merge_commits_only_on_that_branch(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "fcc/w")
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    commit_all(repo, "work")
    git(repo, "checkout", "-q", "main")
    with pytest.raises(GitError, match="Check out fcc/w"):
        reset_working_branch(repo, "fcc/w", base)
    git(repo, "checkout", "-q", "fcc/w")

    assert sorted(reset_working_branch(repo, "fcc/w", base)) == ["README.md", "new.txt"]
    assert git(repo, "rev-parse", "HEAD") == base
    assert not (repo / "new.txt").exists()
    assert (repo / "README.md").read_text(encoding="utf-8") == "base\n"


@pytest.mark.asyncio
async def test_node_sessions_get_policy_budget_memory_and_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _repo(tmp_path)
    sessions = _sessions(tmp_path, launch_policy)
    starts: list[dict[str, object]] = []
    real_start = sessions.start

    async def spy(**kwargs):
        starts.append(kwargs)
        return await real_start(**kwargs)

    monkeypatch.setattr(sessions, "start", spy)
    image: JsonObject = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"},
    }
    graph = TaskGraph("t13", [TaskNode("a", "WRITE a/one.txt 1", write_scope=["a/**"])])
    options = OrchestratorOptions(
        policy_preset="restricted",
        max_turns=7,
        extra_system_prompt="Verified solution patterns: x",
        images=(image,),
    )
    events = await _run(Orchestrator(graph, repo, sessions, options))

    [start] = starts
    assert start["policy_preset"] == "restricted"
    assert start["budget"] == {"max_turns": 7}
    assert start["extra_system_prompt"] == "Verified solution patterns: x"
    [started] = _of(events, "node_started")
    assert started["permission_mode"] == "dontAsk"  # restricted beats acceptEdits
    [done] = _of(events, "node_completed")
    assert str(done["summary"]).endswith("saw 1 images")
