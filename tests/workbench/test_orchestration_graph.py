"""Task graph validation/readiness, planner parsing + fallback, write-scope leases."""

import json
import threading

import pytest

from free_claude_code.workbench.orchestration.graph import (
    TaskGraph,
    TaskNode,
    in_scope,
    scopes_overlap,
)
from free_claude_code.workbench.orchestration.leases import LeaseManager
from free_claude_code.workbench.orchestration.planner import (
    MAX_NODES,
    SYSTEM_PROMPT,
    parse_plan,
    plan,
)


def _graph(*nodes: TaskNode) -> TaskGraph:
    return TaskGraph(task_id="t1", nodes=list(nodes))


# ----- graph -----------------------------------------------------------------


def test_validate_reports_duplicates_missing_self_and_cycles():
    dup = _graph(TaskNode("a", "x"), TaskNode("a", "y"))
    assert dup.validate() == ["duplicate_id:a"]
    missing = _graph(TaskNode("a", "x", depends_on=["zz"]))
    assert missing.validate() == ["missing_dependency:a>zz"]
    self_dep = _graph(TaskNode("a", "x", depends_on=["a"]))
    assert self_dep.validate() == ["self_dependency:a"]
    cycle = _graph(
        TaskNode("a", "x", depends_on=["c"]),
        TaskNode("b", "x", depends_on=["a"]),
        TaskNode("c", "x", depends_on=["b"]),
        TaskNode("d", "x"),
    )
    assert cycle.validate() == ["cycle"]
    assert cycle.order() == ["d"]


def test_order_ready_and_failure_blocks_only_dependents():
    graph = _graph(
        TaskNode("test", "t", depends_on=["impl"]),
        TaskNode("impl", "i"),
        TaskNode("docs", "d"),
        TaskNode("review", "r", depends_on=["test"]),
    )
    assert graph.validate() == []
    assert graph.order() == ["impl", "docs", "test", "review"]
    assert [n.id for n in graph.ready()] == ["impl", "docs"]
    graph.node("impl").status = "completed"
    graph.node("docs").status = "running"
    assert [n.id for n in graph.ready()] == ["test"]
    graph.node("test").status = "failed"
    assert graph.block_failed() == ["review"]
    assert graph.node("review").status == "blocked"
    assert graph.node("docs").status == "running"
    assert graph.block_failed() == []
    assert graph.ancestors("review") == {"test", "impl"}
    with pytest.raises(KeyError):
        graph.node("nope")


def test_transitive_blocking_through_chain():
    graph = _graph(
        TaskNode("a", "x"),
        TaskNode("b", "x", depends_on=["a"]),
        TaskNode("c", "x", depends_on=["b"]),
    )
    graph.node("a").status = "cancelled"
    assert sorted(graph.block_failed()) == ["b", "c"]


def test_json_round_trip_and_strict_parsing():
    node = TaskNode(
        "impl",
        "do it",
        role="ui",
        depends_on=[],
        write_scope=["src/**"],
        acceptance_criteria=["renders"],
        status="completed",
        summary="ok",
        branch="fcc/t1/impl",
        cost_usd=0.5,
        turns=3,
        reverted_out_of_scope=["x.txt"],
    )
    graph = _graph(node, TaskNode("rev", "look", role="reviewer", depends_on=["impl"]))
    wire = json.loads(json.dumps(graph.to_json()))
    assert TaskGraph.from_json(wire) == graph
    assert node.mutating and not graph.node("rev").mutating
    for bad in (
        [],
        {"task_id": "t"},
        {"task_id": "t", "nodes": [{"id": "a", "objective": "x", "role": "boss"}]},
        {"task_id": "t", "nodes": [{"id": "a", "objective": "x", "status": "meh"}]},
        {"task_id": "t", "nodes": [{"id": "", "objective": "x"}]},
        {"task_id": "t", "nodes": [{"id": "a", "objective": "x", "depends_on": "b"}]},
        {"task_id": "t", "nodes": ["a"]},
    ):
        with pytest.raises(ValueError):
            TaskGraph.from_json(bad)


def test_glob_scope_matching_and_overlap():
    assert in_scope("src/a/b.py", ["src/**"])
    assert in_scope("b.py", ["**/*.py"]) and in_scope("x/y/b.py", ["**/*.py"])
    assert not in_scope("src/a/b.py", ["src/*.py"])
    assert in_scope("src\\a.py", ["src/*.py"])
    assert not in_scope("srcx/a.py", ["src/**"])
    assert scopes_overlap(["src/**"], ["src/api/**"])
    assert scopes_overlap(["**"], ["docs/x.md"])
    assert scopes_overlap(["src/*.py"], ["src/a.py"])
    assert scopes_overlap(["a.txt"], ["a.txt"])
    assert not scopes_overlap(["a.txt"], ["b.txt"])
    assert not scopes_overlap(["src/a/**"], ["src/b/**"])
    assert not scopes_overlap(["docs/*.md"], ["src/a.py"])
    assert not scopes_overlap([], ["**"])


# ----- planner ---------------------------------------------------------------


class _Model:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _nodes_json(*nodes: dict) -> str:
    return json.dumps({"nodes": list(nodes)})


@pytest.mark.asyncio
async def test_plan_accepts_valid_json_in_prose_and_passes_contract():
    reply = (
        "Here you go:\n```json\n"
        + _nodes_json(
            {
                "id": "api",
                "objective": "Add endpoint",
                "role": "implementation",
                "depends_on": [],
                "write_scope": ["./src\\api/**"],
                "acceptance_criteria": ["GET /x returns 200"],
            },
            {
                "id": "tests",
                "objective": "Test it",
                "role": "test",
                "depends_on": ["api"],
                "write_scope": ["tests/**"],
                "acceptance_criteria": [],
            },
        )
        + "\n```"
    )
    model = _Model(reply)
    graph = await plan("add x", "MUST: keep y", model, task_id="t9")
    assert graph.task_id == "t9"
    assert [n.id for n in graph.nodes] == ["api", "tests"]
    assert graph.node("api").write_scope == ["src/api/**"]
    system, user = model.calls[0]
    assert system == SYSTEM_PROMPT and '"write_scope"' in system
    assert "add x" in user and "MUST: keep y" in user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "no json here",
        "[1, 2]",
        '{"nodes": []}',
        '{"nodes": [{"id": "a"}]}',
        _nodes_json({"id": "a", "objective": "x", "depends_on": ["b"]}),
        _nodes_json(
            {"id": "a", "objective": "x", "depends_on": ["b"]},
            {"id": "b", "objective": "x", "depends_on": ["a"]},
        ),
        _nodes_json({"id": "a", "objective": "x", "write_scope": ["/etc/**"]}),
        _nodes_json({"id": "a", "objective": "x", "write_scope": ["../x/**"]}),
        _nodes_json({"id": "a", "objective": "x", "write_scope": ["C:/x"]}),
        _nodes_json({"id": "Bad Id", "objective": "x"}),
        _nodes_json({"id": "a", "objective": "x", "status": "completed"}),
        RuntimeError("endpoint down"),
    ],
)
async def test_plan_falls_back_to_single_implementation_node(reply):
    graph = await plan("fix the bug", None, _Model(reply))
    assert len(graph.nodes) == 1
    only = graph.nodes[0]
    assert (only.id, only.role, only.objective) == (
        "implementation",
        "implementation",
        "fix the bug",
    )
    assert only.write_scope == ["**"] and graph.task_id


def test_oversized_plan_is_trimmed_and_orphans_dropped():
    nodes: list[dict[str, str | list[str]]] = [
        {"id": f"n{i}", "objective": "x"} for i in range(MAX_NODES + 3)
    ]
    graph = parse_plan(json.dumps({"nodes": nodes}), "t")
    assert len(graph.nodes) == MAX_NODES
    # The 7th node is dropped, so the kept node depending on it is dropped as well.
    nodes[1]["depends_on"] = [f"n{MAX_NODES}"]
    nodes[2]["depends_on"] = ["n1"]
    trimmed = parse_plan(json.dumps({"nodes": nodes}), "t")
    assert [n.id for n in trimmed.nodes] == ["n0", "n3", "n4", "n5"]


# ----- leases ----------------------------------------------------------------


def test_leases_conflict_release_reacquire_and_expiry():
    now = [0.0]
    leases = LeaseManager(clock=lambda: now[0])
    first = leases.try_acquire("t", "a", ["src/**"], ttl_s=10)
    assert first is not None
    assert leases.try_acquire("t", "b", ["src/api/**"], ttl_s=10) is None
    assert leases.try_acquire("t", "c", ["docs/**"], ttl_s=10) is not None
    # The same holder may re-acquire (replaces its own lease).
    again = leases.try_acquire("t", "a", ["src/**"], ttl_s=10)
    assert again is not None and len(leases.active()) == 2
    leases.release(again)
    leases.release(again)  # idempotent
    assert leases.try_acquire("t", "b", ["src/api/**"], ttl_s=10) is not None
    # A conflicting re-acquire fails without dropping the holder's existing lease.
    assert leases.try_acquire("t", "c", ["src/**"], ttl_s=10) is None
    assert any(held.node_id == "c" for held in leases.active())
    now[0] = 11.0
    assert leases.active() == []
    assert leases.try_acquire("u", "z", ["**"], ttl_s=1) is not None


def test_concurrent_acquire_has_exactly_one_winner():
    leases = LeaseManager()
    barrier = threading.Barrier(16)
    wins: list[str] = []

    def contend(i: int) -> None:
        barrier.wait()
        if leases.try_acquire("t", f"n{i}", ["src/**"], ttl_s=60):
            wins.append(f"n{i}")

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(wins) == 1
