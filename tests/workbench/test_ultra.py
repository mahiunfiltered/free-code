"""Ultra mode: lead-agent analysis, snapshot/in-place orchestration, synthesis, runs."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio

from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.cli.managed.interactive import (
    InteractiveClaudeSession,
    InteractiveClaudeSessions,
)
from free_claude_code.core.json_types import JsonObject
from free_claude_code.core.storage import Store
from free_claude_code.workbench import coordinator as coordinator_module
from free_claude_code.workbench.audit import AuditLog
from free_claude_code.workbench.checkpoints import create_checkpoint
from free_claude_code.workbench.coordinator import CoordinatorOptions, TaskCoordinator
from free_claude_code.workbench.orchestration import resources
from free_claude_code.workbench.orchestration.graph import TaskGraph, TaskNode
from free_claude_code.workbench.orchestration.integration import (
    IntegrationResult,
    apply_to_worktree,
    changed_paths,
    scan_tree,
)
from free_claude_code.workbench.orchestration.planner import (
    ANALYZE_PROMPT,
    analyze,
    parse_analysis,
)
from free_claude_code.workbench.orchestration.resources import memory_parallel_cap
from free_claude_code.workbench.orchestration.runner import (
    Orchestrator,
    OrchestratorOptions,
)
from free_claude_code.workbench.orchestration.worktrees import (
    commit_all,
    create_worktree,
    current_branch,
    git,
)
from free_claude_code.workbench.tasks import TaskStore
from free_claude_code.workbench.ultra import REPORT_TAG, synthesis_prompt
from tests.workbench.test_coordinator import (
    FIX,
    fake_claude,
    make_repo,
    obj,
    pytest_profile,
    sent_messages,
)
from tests.workbench.test_orchestration_git import _of, _repo, _run, _sessions

# ----- analysis ----------------------------------------------------------------


class Model:
    """Scripted lead-agent model; ``delay`` simulates a slow endpoint."""

    def __init__(self, reply: str | Exception, delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        await asyncio.sleep(self.delay)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def node(node_id: str, instructions: str, scope: list[str], **extra) -> dict:
    return {
        "id": node_id,
        "role": "implementation",
        "objective": f"build {node_id}",
        "instructions": instructions,
        "write_scope": scope,
        "depends_on": [],
        "acceptance_criteria": [f"{node_id} exists"],
        **extra,
    }


def orchestrate(*nodes: dict) -> str:
    return "Plan:\n" + json.dumps(
        {"route": "orchestrate", "reason": "independent modules", "nodes": list(nodes)}
    )


def test_parse_analysis_direct_orchestrate_and_single_node():
    direct = parse_analysis('{"route": "direct", "reason": " a question "}', "t")
    assert direct.route == "direct" and direct.reason == "a question"
    assert direct.graph is None and not direct.degraded

    two = parse_analysis(
        orchestrate(
            node("a", "Create a.py with f()", ["a.py"]),
            node("b", "Create b.py", ["b.py"], depends_on=["a"]),
        ),
        "t1",
    )
    assert two.route == "orchestrate" and two.graph is not None
    assert two.graph.task_id == "t1"
    assert two.graph.node("a").instructions == "Create a.py with f()"
    assert two.graph.node("b").depends_on == ["a"]
    assert two.graph.node("a").to_json()["instructions"] == "Create a.py with f()"

    # A single sub-task only adds overhead: run it directly.
    one = parse_analysis(orchestrate(node("a", "x", ["a.py"])), "t")
    assert one.route == "direct" and one.graph is None


@pytest.mark.parametrize(
    "reply",
    [
        "no json",
        '{"route": "sideways"}',
        '{"route": "orchestrate", "nodes": []}',
        orchestrate(node("a", "x", ["/etc/**"]), node("b", "y", ["b/**"])),
        orchestrate(
            node("a", "x", [], depends_on=["b"]), node("b", "y", [], depends_on=["a"])
        ),
    ],
)
def test_parse_analysis_rejects_unusable_replies(reply: str):
    with pytest.raises(ValueError):
        parse_analysis(reply, "t")


@pytest.mark.asyncio
async def test_analyze_falls_back_to_direct_on_no_model_error_timeout_and_garbage():
    none = await analyze("hi", None, task_id="t")
    assert none.route == "direct" and none.degraded

    broken = await analyze("hi", Model(RuntimeError("503")), task_id="t")
    assert broken.route == "direct" and broken.degraded and "503" in broken.reason

    slow = await analyze(
        "hi", Model('{"route": "direct"}', 5), task_id="t", timeout_s=0.1
    )
    assert slow.route == "direct" and slow.degraded and "TimeoutError" in slow.reason

    garbage = await analyze("hi", Model("sure!"), task_id="t")
    assert garbage.route == "direct" and garbage.degraded

    model = Model(orchestrate(node("a", "x", ["a/**"]), node("b", "y", ["b/**"])))
    ok = await analyze("split it", model, task_id="t")
    assert ok.route == "orchestrate" and not ok.degraded
    assert model.calls == [(ANALYZE_PROMPT, "split it")]
    assert '"instructions"' in ANALYZE_PROMPT and '"direct"' in ANALYZE_PROMPT


# ----- snapshot workspace: dirty git tree ---------------------------------------


def _snapshot_node(
    repo: Path, base: str, node_id: str, files: dict[str, str | None]
) -> TaskNode:
    """A completed node whose worktree (from ``base``) changed ``files``."""

    wt = create_worktree(repo, "u1", node_id, base)
    item = TaskNode(node_id, "x", write_scope=["**"], status="completed")
    item.branch, item.worktree, item.start_commit = wt.branch, str(wt.path), base
    for rel, content in files.items():
        target = wt.path / rel
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    commit_all(wt.path, node_id)
    return item


def test_apply_to_worktree_merges_into_a_dirty_tree_and_reports_conflicts(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    (repo / "notes.md").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-q", "-m", "notes")
    # Dirty tree: an edited tracked file and an untracked one.
    (repo / "README.md").write_text("user edit\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("mine\n", encoding="utf-8")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = create_checkpoint(repo).commit
    assert snapshot

    fresh = _snapshot_node(repo, snapshot, "fresh", {"new/mod.py": "x = 1\n"})
    # The snapshot carries the user's untracked file into the worktree.
    assert (Path(str(fresh.worktree)) / "scratch.txt").read_text("utf-8") == "mine\n"
    merged = _snapshot_node(
        repo, snapshot, "merged", {"notes.md": "l1\nl2\nl3\nNODE\n"}
    )
    clash = _snapshot_node(repo, snapshot, "clash", {"README.md": "node edit\n"})
    removed = _snapshot_node(repo, snapshot, "removed", {"src/app.py": None})
    # After the snapshot the user keeps editing: notes line 1 (mergeable) and
    # README (conflicts with "clash").
    (repo / "notes.md").write_text("USER\nl2\nl3\nl4\n", encoding="utf-8")
    (repo / "README.md").write_text("user edit 2\n", encoding="utf-8")

    result = apply_to_worktree(repo, TaskGraph("u1", [fresh, merged, clash, removed]))

    assert result.merged == ["fresh", "merged", "removed"]
    assert result.conflicts == {"clash": ["README.md"]}
    assert result.status == "conflicts"
    assert (repo / "new" / "mod.py").read_text("utf-8") == "x = 1\n"
    assert (repo / "notes.md").read_text("utf-8") == "USER\nl2\nl3\nNODE\n"
    assert (repo / "README.md").read_text("utf-8") == "user edit 2\n"  # untouched
    assert not (repo / "src" / "app.py").exists()
    assert (repo / "scratch.txt").read_text("utf-8") == "mine\n"
    assert "new/mod.py" in result.final_diff_stat
    # Never committed, staged, or switched anything.
    assert git(repo, "rev-parse", "HEAD") == head
    assert current_branch(repo) == "main"
    assert git(repo, "diff", "--cached", "--name-only") == ""


@pytest.mark.asyncio
async def test_snapshot_orchestration_applies_node_work_to_a_dirty_tree(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("u\n", encoding="utf-8")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = create_checkpoint(repo).commit
    graph = TaskGraph(
        "s1",
        [
            TaskNode(
                "a",
                "add a",
                instructions="WRITE a/one.txt 1\nWRITE stray.txt s",
                write_scope=["a/**"],
            ),
            TaskNode(
                "b", "add b", instructions="WRITE b/two.txt 2", write_scope=["b/**"]
            ),
        ],
    )
    orchestrator = Orchestrator(
        graph,
        repo,
        _sessions(tmp_path),
        OrchestratorOptions(workspace="snapshot", base_snapshot=snapshot),
    )
    events = await _run(orchestrator)

    assert events[0]["workspace"] == "snapshot" and events[0]["base_commit"] == snapshot
    completed = {e["node_id"]: e for e in _of(events, "node_completed")}
    assert completed["a"]["changed_files"] == ["a/one.txt"]
    assert completed["a"]["reverted_out_of_scope"] == ["stray.txt"]
    assert isinstance(completed["a"]["elapsed_s"], float)
    [integration] = _of(events, "integration_completed")
    assert integration["merged"] == ["a", "b"] and integration["conflicts"] == {}
    assert events[-1]["status"] == "ready_for_verification"
    assert (repo / "a" / "one.txt").read_text("utf-8") == "1\n"
    assert (repo / "b" / "two.txt").read_text("utf-8") == "2\n"
    assert not (repo / "stray.txt").exists()
    assert (repo / "README.md").read_text("utf-8") == "dirty\n"
    assert (repo / "untracked.txt").exists()
    assert git(repo, "rev-parse", "HEAD") == head and current_branch(repo) == "main"
    assert orchestrator.working_branch is None
    assert "Instructions from the lead agent:" in orchestrator._prompt(graph.node("a"))


@pytest.mark.asyncio
async def test_snapshot_workspace_requires_a_snapshot(tmp_path: Path):
    graph = TaskGraph("s2", [TaskNode("a", "x", write_scope=["a/**"])])
    orchestrator = Orchestrator(
        graph,
        _repo(tmp_path),
        _sessions(tmp_path),
        OrchestratorOptions(workspace="snapshot"),
    )
    [event] = await _run(orchestrator)
    assert event["type"] == "orchestration_failed"


# ----- in-place workspace: no git ------------------------------------------------


def test_scan_tree_and_changed_paths(tmp_path: Path):
    (tmp_path / "a.txt").write_text("1", "utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x", "utf-8")
    before = scan_tree(tmp_path)
    assert list(before) == ["a.txt"]
    (tmp_path / "a.txt").write_text("22", "utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b", "utf-8")
    assert changed_paths(before, scan_tree(tmp_path)) == ["a.txt", "sub/b.txt"]


@pytest.mark.asyncio
async def test_in_place_disjoint_nodes_run_concurrently_and_report_out_of_scope(
    tmp_path: Path,
):
    folder = tmp_path / "plain"
    folder.mkdir()
    sessions = _sessions(tmp_path)
    graph = TaskGraph(
        "p1",
        [
            # "a" waits on a permission prompt until "b" is done: both run at once.
            TaskNode(
                "a",
                "x",
                instructions="ASK Bash\nWRITE a/one.txt 1\nWRITE loose.txt l",
                write_scope=["a/**"],
            ),
            TaskNode("b", "y", instructions="WRITE b/two.txt 2", write_scope=["b/**"]),
        ],
    )
    orchestrator = Orchestrator(
        graph,
        folder,
        sessions,
        OrchestratorOptions(workspace="in_place", max_parallel=2),
    )
    events: list[JsonObject] = []
    asked: str | None = None
    b_done = False

    async def collect() -> None:
        nonlocal asked, b_done
        async for event in orchestrator.run():
            events.append(event)
            if event["type"] == "node_permission":
                asked = str(event["live_id"])
            if event["type"] == "node_completed" and event["node_id"] == "b":
                b_done = True
            if asked and b_done:
                live = sessions.get(asked)
                assert live is not None
                await live.respond_permission("perm_Bash", {"behavior": "allow"})
                asked = None

    await asyncio.wait_for(collect(), 60)

    started = [e["node_id"] for e in _of(events, "node_started")]
    first_done = [e["type"] for e in events].index("node_completed")
    assert set(started) == {"a", "b"}
    assert all(
        events.index(e) < first_done for e in _of(events, "node_started")
    )  # concurrent
    assert {e["cwd"] for e in _of(events, "node_started")} == {str(folder.resolve())}
    completed = {e["node_id"]: e for e in _of(events, "node_completed")}
    assert completed["a"]["changed_files"] == ["a/one.txt"]
    assert completed["a"]["out_of_scope"] == ["loose.txt"]
    assert completed["b"]["changed_files"] == ["b/two.txt"]
    assert completed["b"]["out_of_scope"] == []
    assert not _of(events, "integration_started")
    assert events[-1]["status"] == "ready_for_verification"
    assert (folder / "loose.txt").exists()  # no git: reported, not reverted
    assert (folder / "a" / "one.txt").exists() and (folder / "b" / "two.txt").exists()


@pytest.mark.asyncio
async def test_in_place_overlapping_scopes_serialize(tmp_path: Path):
    folder = tmp_path / "plain"
    folder.mkdir()
    graph = TaskGraph(
        "p2",
        [
            TaskNode(
                "a", "x", instructions="WRITE src/a.txt 1", write_scope=["src/**"]
            ),
            TaskNode(
                "b", "y", instructions="WRITE src/b.txt 2", write_scope=["src/b.txt"]
            ),
        ],
    )
    events = await _run(
        Orchestrator(
            graph,
            folder,
            _sessions(tmp_path),
            OrchestratorOptions(workspace="in_place", max_parallel=4),
        )
    )
    kinds = [(e["type"], e.get("node_id")) for e in events]
    assert kinds.index(("node_completed", "a")) < kinds.index(("node_started", "b"))
    completed = {e["node_id"]: e for e in _of(events, "node_completed")}
    assert completed["a"]["changed_files"] == ["src/a.txt"]
    assert completed["b"]["changed_files"] == ["src/b.txt"]


# ----- synthesis -----------------------------------------------------------------


def test_synthesis_prompt_reports_every_sub_task():
    done = TaskNode("api", "Build the API", status="completed", summary="added /x")
    done.changed_files = ["src/api.py"]
    done.reverted_out_of_scope = ["README.md"]
    failed = TaskNode("ui", "Build the UI", status="failed", error="timed out")
    loose = TaskNode("docs", "Docs", status="completed", out_of_scope=["tmp.txt"])
    integration = IntegrationResult(merged=["api"], conflicts={"docs": ["a.md"]})

    text = synthesis_prompt(
        "Make an app",
        TaskGraph("t", [done, failed, loose]),
        integration,
        ["/r/.fcc-worktrees/t/docs"],
        task_id="T1",
        workspace="snapshot",
    )

    assert text.startswith(
        f'<{REPORT_TAG} task_id="T1">\n<user_request>\nMake an app\n'
    )
    assert "into 3 sub-tasks" in text and "applied to the working tree" in text
    assert "## api (implementation) - completed" in text
    assert "Files changed: src/api.py" in text and "Report: added /x" in text
    assert "Out-of-scope edits reverted: README.md" in text
    assert "## ui (implementation) - failed" in text and "Error: timed out" in text
    assert "(left in place): tmp.txt" in text
    assert "- applied: api" in text and "CONFLICT, not applied for docs: a.md" in text
    assert "kept for manual review in: /r/.fcc-worktrees/t/docs" in text
    assert text.index(f"</{REPORT_TAG}>") < text.index("Now write the final answer")


# ----- coordinator ---------------------------------------------------------------


@dataclass
class Env:
    tmp: Path
    tasks: TaskStore
    sessions: InteractiveClaudeSessions
    coordinator: TaskCoordinator
    model: Model | None = None
    events: list[JsonObject] = field(default_factory=list)

    def phases(self) -> list[str]:
        return [str(e["phase"]) for e in self.events if e["type"] == "fcc_ultra"]

    def of(self, kind: str) -> list[JsonObject]:
        return [e for e in self.events if e["type"] == kind]


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    store = Store(tmp_path / "fcc.db")
    tasks = TaskStore(store)
    sessions = InteractiveClaudeSessions(
        proxy_target=lambda: ("http://127.0.0.1:1", "token"),
        claude_bin=fake_claude(tmp_path),
    )
    holder: list[Env] = []
    coordinator = TaskCoordinator(
        tasks=tasks,
        audit=AuditLog(store),
        sessions=sessions,
        model_client_factory=lambda model: holder[0].model,
        options=CoordinatorOptions(ultra_dispatch_delay_s=0, command_timeout_s=120),
    )
    holder.append(Env(tmp_path, tasks, sessions, coordinator))
    yield holder[0]
    await coordinator.close()
    await sessions.stop_all()
    store.close()


async def _session(env: Env, cwd: Path) -> InteractiveClaudeSession:
    return await env.sessions.start(cwd=str(cwd), permission_mode="acceptEdits")


def _task_status(env: Env, status: str) -> str:
    [task_id] = {str(e["task_id"]) for e in env.events}
    assert env.tasks.get(task_id).status == status
    return task_id


@pytest.mark.asyncio
async def test_ultra_direct_fast_path_sends_the_prompt_as_is(env: Env):
    repo = make_repo(env.tmp)
    session = await _session(env, repo)
    env.model = Model('{"route": "direct", "reason": "a small edit"}')

    status = await env.coordinator.run_ultra(
        session, f"Fix add\n{FIX}", publish=env.events.append
    )

    assert status == "COMPLETED"
    task_id = _task_status(env, "COMPLETED")
    assert env.phases() == ["analyzing", "direct", "done"]
    direct = env.of("fcc_ultra")[1]
    assert direct["route"] == "direct" and direct["reason"] == "a small edit"
    assert isinstance(direct["analysis_ms"], int)
    assert not env.of("fcc_orchestration")
    assert await sent_messages(session) == [f"Fix add\n{FIX}"]
    assert "return a + b" in (repo / "calc.py").read_text("utf-8")
    assert [e["status"] for e in env.of("fcc_task")] == [
        "RECEIVED",
        "RUNNING",
        "COMPLETED",
    ]
    analysis = [e for e in env.tasks.events(task_id) if e.type == "ultra.analysis"]
    assert analysis[0].payload["route"] == "direct"


@pytest.mark.asyncio
async def test_ultra_analysis_timeout_runs_directly(env: Env):
    env.coordinator.options.ultra_analysis_timeout_s = 0.1
    env.model = Model(
        orchestrate(node("a", "x", ["a/**"]), node("b", "y", ["b/**"])), 5
    )
    session = await _session(env, make_repo(env.tmp))

    assert await env.coordinator.run_ultra(
        session, "hello", publish=env.events.append
    ) == ("COMPLETED")
    direct = env.of("fcc_ultra")[1]
    assert direct["phase"] == "direct" and direct["degraded"] is True


@pytest.mark.asyncio
async def test_ultra_orchestrates_a_dirty_repo_and_the_chat_synthesizes(env: Env):
    repo = make_repo(env.tmp)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "notes.txt").write_text("user work\n", "utf-8")  # untracked: dirty tree
    head = git(repo, "rev-parse", "HEAD")
    session = await _session(env, repo)
    env.model = Model(
        orchestrate(
            node(
                "strings",
                "WRITE strings_util.py def slugify(s): return s",
                ["strings_util.py"],
            ),
            node(
                "maths", "WRITE math_util.py def clamp(x): return x", ["math_util.py"]
            ),
        )
    )

    status = await env.coordinator.run_ultra(
        session, "Create two modules", publish=env.events.append, max_parallel=2
    )

    assert status == "COMPLETED"
    _task_status(env, "COMPLETED")
    assert env.phases() == [
        "analyzing",
        "planned",
        "dispatching",
        "integrating",
        "summarizing",
        "done",
    ]
    planned = env.of("fcc_ultra")[1]
    assert planned["workspace"] == "snapshot" and planned["max_parallel"] == 2
    plan_nodes = obj(planned["plan"])["nodes"]
    assert isinstance(plan_nodes, list) and len(plan_nodes) == 2
    assert str(obj(plan_nodes[0])["instructions"]).startswith("WRITE strings_util.py")
    assert (repo / "strings_util.py").exists() and (repo / "math_util.py").exists()
    assert (repo / "notes.txt").read_text("utf-8") == "user work\n"
    assert git(repo, "rev-parse", "HEAD") == head
    assert current_branch(repo) == "feature"
    orchestration = [obj(e["event"])["type"] for e in env.of("fcc_orchestration")]
    assert "integration_completed" in orchestration
    assert all(isinstance(e["ts"], float) for e in env.of("fcc_orchestration"))
    # The chat only saw the synthesis report (with the request), not the plan.
    [report] = await sent_messages(session)
    assert isinstance(report, str) and report.startswith(f"<{REPORT_TAG}")
    assert (
        "Create two modules" in report
        and "## strings (implementation) - completed" in report
    )
    assert "Files changed: strings_util.py" in report


@pytest.mark.asyncio
async def test_ultra_orchestrates_in_place_outside_git(env: Env):
    folder = env.tmp / "plain"
    folder.mkdir()
    session = await _session(env, folder)
    env.model = Model(
        orchestrate(
            node("a", "WRITE a/one.txt 1", ["a/**"]),
            node("b", "WRITE b/two.txt 2", ["b/**"]),
        )
    )

    status = await env.coordinator.run_ultra(
        session, "Two parts", publish=env.events.append
    )

    assert status == "COMPLETED"
    assert env.of("fcc_ultra")[1]["workspace"] == "in_place"
    assert env.of("fcc_ultra")[1]["max_parallel"] == 4  # the default
    assert (folder / "a" / "one.txt").exists() and (folder / "b" / "two.txt").exists()
    assert "integrating" not in env.phases()
    assert env.of("fcc_checkpoint")[0]["supported"] is False


@pytest.mark.asyncio
async def test_ultra_verify_runs_the_gate_on_the_integrated_result(env: Env):
    repo = make_repo(env.tmp)
    env.coordinator.options.profile = pytest_profile(repo)
    session = await _session(env, repo)
    env.model = Model(
        orchestrate(
            node("calc", FIX, ["calc.py"]),
            node("review", "Review calc.py", [], role="reviewer"),
        )
    )

    status = await env.coordinator.run_ultra(
        session,
        "Fix add in calc.py so the tests pass",
        publish=env.events.append,
        verify=True,
    )

    assert status == "VERIFIED"
    assert env.phases()[-3:] == ["summarizing", "verifying", "done"]
    assert env.of("fcc_verification")[0]["disposition"] == "VERIFIED"


@pytest.mark.asyncio
async def test_ultra_plan_countdown_can_be_cancelled(env: Env):
    env.coordinator.options.ultra_dispatch_delay_s = 30
    session = await _session(env, make_repo(env.tmp))
    env.model = Model(orchestrate(node("a", "x", ["a/**"]), node("b", "y", ["b/**"])))

    task_id = env.coordinator.start(session, "Two parts", mode="ultra")
    for _ in range(200):
        events = [e.type for e in env.tasks.events(task_id)]
        if "checkpoint.created" in events:
            break
        await asyncio.sleep(0.05)
    assert await env.coordinator.cancel(task_id)
    assert env.tasks.get(task_id).status == "CANCELLED"
    assert await sent_messages(session) == []


@pytest.mark.asyncio
async def test_ultra_start_validates_max_parallel(env: Env):
    session = await _session(env, make_repo(env.tmp))
    for bad in (0, 7):
        with pytest.raises(InvalidRequestError, match="max_parallel"):
            env.coordinator.start(session, "x", mode="ultra", max_parallel=bad)


@pytest.mark.asyncio
async def test_ultra_prefers_the_dedicated_analysis_client(env: Env):
    analysis = Model('{"route": "direct", "reason": "fast lead"}')
    general = Model('{"route": "orchestrate"}')
    store = Store(env.tmp / "audit.db")
    coordinator = TaskCoordinator(
        tasks=env.tasks,
        audit=AuditLog(store),
        sessions=env.sessions,
        model_client_factory=lambda model: general,
        analysis_client_factory=lambda: analysis,
        options=CoordinatorOptions(ultra_dispatch_delay_s=0),
    )
    session = await _session(env, make_repo(env.tmp))
    try:
        status = await coordinator.run_ultra(session, "hi", publish=env.events.append)
    finally:
        await coordinator.close()
        store.close()

    assert status == "COMPLETED"
    assert analysis.calls == [(ANALYZE_PROMPT, "hi")] and general.calls == []
    assert env.of("fcc_ultra")[1]["reason"] == "fast lead"


@pytest.mark.asyncio
async def test_ultra_plan_reports_memory_limited_parallelism(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    caps: list[int] = []

    def cap(requested: int) -> int:
        caps.append(requested)
        return 1

    monkeypatch.setattr(coordinator_module, "memory_parallel_cap", cap)
    folder = env.tmp / "plain"
    folder.mkdir()
    session = await _session(env, folder)
    env.model = Model(
        orchestrate(
            node("a", "WRITE a/one.txt 1", ["a/**"]),
            node("b", "WRITE b/two.txt 2", ["b/**"]),
        )
    )

    status = await env.coordinator.run_ultra(
        session, "Two parts", publish=env.events.append, max_parallel=3
    )

    assert status == "COMPLETED"
    planned = env.of("fcc_ultra")[1]
    assert caps == [3]
    assert planned["max_parallel"] == 3 and planned["effective_parallel"] == 1
    assert planned["parallel_limited_by"] == "memory"
    started = obj(env.of("fcc_orchestration")[0]["event"])
    assert started["type"] == "orchestration_started" and started["max_parallel"] == 1


@pytest.mark.asyncio
async def test_ultra_plan_is_not_memory_limited_with_room(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(coordinator_module, "memory_parallel_cap", lambda n: n)
    folder = env.tmp / "plain"
    folder.mkdir()
    session = await _session(env, folder)
    env.model = Model(
        orchestrate(node("a", "WRITE a/1 1", ["a/**"]), node("b", "x", ["b/**"]))
    )
    await env.coordinator.run_ultra(session, "Two", publish=env.events.append)
    planned = env.of("fcc_ultra")[1]
    assert planned["effective_parallel"] == planned["max_parallel"] == 4
    assert planned["parallel_limited_by"] is None


def test_memory_parallel_cap_keeps_headroom_and_at_least_one(
    monkeypatch: pytest.MonkeyPatch,
):
    gib = 1024**3
    assert memory_parallel_cap(4, available=8 * gib) == 4  # plenty
    # 2.5 GB free - 1.5 GB headroom = 1 GB -> two 400 MB agents.
    assert memory_parallel_cap(4, available=int(2.5 * gib)) == 2
    assert memory_parallel_cap(4, available=1 * gib) == 1  # never below one
    free = resources.available_memory_bytes()
    assert free is None or free > 0  # the real platform probe
    monkeypatch.setattr(resources, "available_memory_bytes", lambda: None)
    assert memory_parallel_cap(5) == 5  # unknown: as requested


# ----- crash retry ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_crashed_node_is_retried_once_in_a_reset_worktree(tmp_path: Path):
    repo = _repo(tmp_path)
    snapshot = create_checkpoint(repo).commit
    marker = tmp_path / "crashed-once"
    graph = TaskGraph(
        "r1",
        [
            TaskNode(
                "a",
                "x",
                instructions=f"WRITE a/one.txt 1\nCRASH_ONCE {marker}",
                write_scope=["a/**"],
            )
        ],
    )
    orchestrator = Orchestrator(
        graph,
        repo,
        _sessions(tmp_path),
        OrchestratorOptions(workspace="snapshot", base_snapshot=snapshot),
    )
    events = await _run(orchestrator)

    [retry] = _of(events, "node_retry")
    assert retry["node_id"] == "a" and retry["attempt"] == 2
    assert retry["reset"] == "worktree" and retry["partial_files"] == []
    assert "code 3" in str(retry["error"]) or "without a result" in str(retry["error"])
    assert [e["attempt"] for e in _of(events, "node_started")] == [1, 2]
    [done] = _of(events, "node_completed")
    assert done["changed_files"] == ["a/one.txt"]
    assert events[-1]["status"] == "ready_for_verification"
    assert (repo / "a" / "one.txt").exists()


def test_reset_for_retry_restores_the_worktree_to_its_start(tmp_path: Path):
    repo = _repo(tmp_path)
    wt = create_worktree(repo, "r3", "a", git(repo, "rev-parse", "HEAD"))
    item = TaskNode("a", "x", write_scope=["**"])
    item.worktree, item.start_commit = str(wt.path), git(wt.path, "rev-parse", "HEAD")
    (wt.path / "README.md").write_text("crashed edit\n", "utf-8")
    (wt.path / "half").mkdir()
    (wt.path / "half" / "new.txt").write_text("partial\n", "utf-8")
    orchestrator = Orchestrator(TaskGraph("r3", [item]), repo, _sessions(tmp_path))

    assert orchestrator._reset_for_retry(item, wt.path, {}) == []
    assert (wt.path / "README.md").read_text("utf-8") == "base\n"
    assert not (wt.path / "half").exists()


@pytest.mark.asyncio
async def test_in_place_crash_retry_notes_partial_files_and_second_crash_fails(
    tmp_path: Path,
):
    folder = tmp_path / "plain"
    folder.mkdir()
    marker = tmp_path / "once"
    graph = TaskGraph(
        "r2",
        [
            TaskNode(
                "a",
                "x",
                instructions=f"WRITE a/half.txt h\nCRASH_ONCE {marker}",
                write_scope=["a/**"],
            ),
            TaskNode("b", "y", instructions="CRASH", write_scope=["b/**"]),
            TaskNode("c", "z", instructions="FAIL", write_scope=["c/**"]),
        ],
    )
    events = await _run(
        Orchestrator(
            graph,
            folder,
            _sessions(tmp_path),
            OrchestratorOptions(workspace="in_place", max_parallel=3),
        )
    )

    retries = {e["node_id"]: e for e in _of(events, "node_retry")}
    assert set(retries) == {"a", "b"}  # an error result ("c") is never retried
    assert retries["a"]["reset"] == "none"
    assert retries["a"]["partial_files"] == ["a/half.txt"]
    assert [e["node_id"] for e in _of(events, "node_completed")] == ["a"]
    failed = {e["node_id"] for e in _of(events, "node_failed")}
    assert failed == {"b", "c"}
    started = [e["node_id"] for e in _of(events, "node_started")]
    assert started.count("a") == 2 and started.count("b") == 2
    assert started.count("c") == 1


@pytest.mark.asyncio
async def test_permission_timeout_is_not_retried(tmp_path: Path):
    folder = tmp_path / "plain"
    folder.mkdir()
    graph = TaskGraph(
        "r4", [TaskNode("a", "x", instructions="ASK Bash", write_scope=["a/**"])]
    )
    events = await _run(
        Orchestrator(
            graph,
            folder,
            _sessions(tmp_path),
            OrchestratorOptions(workspace="in_place", permission_timeout_s=0.5),
        )
    )
    assert not _of(events, "node_retry")
    [failed] = _of(events, "node_failed")
    assert "timed out waiting for approval of Bash" in str(failed["error"])


# ----- shared ignore rules ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_state_files_are_never_reverted_applied_or_out_of_scope(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    (repo / ".fcc").mkdir()
    (repo / ".fcc" / "verify.json").write_text('{"ignore": ["logs/**"]}', "utf-8")
    (repo / ".impeccable").mkdir()
    (repo / ".impeccable" / "state.json").write_text("user\n", "utf-8")
    snapshot = create_checkpoint(repo).commit
    graph = TaskGraph(
        "i1",
        [
            TaskNode(
                "a",
                "x",
                instructions=(
                    "WRITE a/one.txt 1\nWRITE .impeccable/state.json node\n"
                    "WRITE .claude-flow/policy/state.json {}\nWRITE logs/run.log l"
                ),
                write_scope=["a/**"],
            )
        ],
    )
    events = await _run(
        Orchestrator(
            graph,
            repo,
            _sessions(tmp_path),
            OrchestratorOptions(workspace="snapshot", base_snapshot=snapshot),
        )
    )

    [done] = _of(events, "node_completed")
    assert done["changed_files"] == ["a/one.txt"]
    assert done["reverted_out_of_scope"] == []
    assert events[-1]["ignored_files"] == [
        ".claude-flow/policy/state.json",
        ".impeccable/state.json",
        "logs/run.log",
    ]
    assert (repo / "a" / "one.txt").exists()
    # The user's tool state is untouched and the node's is not applied.
    assert (repo / ".impeccable" / "state.json").read_text("utf-8") == "user\n"
    assert not (repo / ".claude-flow").exists() and not (repo / "logs").exists()


@pytest.mark.asyncio
async def test_in_place_tool_state_is_not_reported_out_of_scope(tmp_path: Path):
    folder = tmp_path / "plain"
    folder.mkdir()
    graph = TaskGraph(
        "i2",
        [
            TaskNode(
                "a",
                "x",
                instructions="WRITE a/one.txt 1\nWRITE .impeccable/s.json x\nWRITE stray.txt s",
                write_scope=["a/**"],
            )
        ],
    )
    events = await _run(
        Orchestrator(
            graph,
            folder,
            _sessions(tmp_path),
            OrchestratorOptions(workspace="in_place"),
        )
    )

    [done] = _of(events, "node_completed")
    assert done["changed_files"] == ["a/one.txt"]
    assert done["out_of_scope"] == ["stray.txt"]
    assert events[-1]["ignored_files"] == [".impeccable/s.json"]


def test_synthesis_prompt_lists_ignored_tool_state_once():
    done = TaskNode("api", "Build the API", status="completed", summary="ok")
    text = synthesis_prompt(
        "Make an app",
        TaskGraph("t", [done]),
        None,
        [],
        task_id="T2",
        workspace="snapshot",
        ignored=[".impeccable/state.json"],
    )
    assert text.count(".impeccable/state.json") == 1
    assert "Ignored tool-state files" in text
