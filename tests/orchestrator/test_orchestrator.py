import asyncio

import pytest

from free_claude_code.orchestrator import (
    AgentRole,
    AutonomousMasterAgent,
    ConflictManager,
    DependencyKind,
    Task,
    TaskScheduler,
    TaskState,
    WorkerPool,
)


@pytest.mark.asyncio
async def test_worker_pool_concurrency():
    """Verify that worker pool enforces max_parallel_agents limit."""
    pool = WorkerPool(max_parallel_agents=2)
    active_count = 0
    max_observed_active = 0
    lock = asyncio.Lock()

    async def mock_work(task: Task):
        nonlocal active_count, max_observed_active
        async with lock:
            active_count += 1
            if active_count > max_observed_active:
                max_observed_active = active_count
        await asyncio.sleep(0.1)
        async with lock:
            active_count -= 1
        return f"done_{task.task_id}"

    tasks = [Task(task_id=f"t{i}", name=f"Task {i}", description="") for i in range(5)]

    results = await asyncio.gather(*(pool.execute_task(t, mock_work) for t in tasks))
    assert max_observed_active <= 2
    assert all(t.status == TaskState.COMPLETED for t in results)


@pytest.mark.asyncio
async def test_conflict_manager_scope_locking():
    """Verify that conflict manager prevents concurrent overlapping path writes."""
    cm = ConflictManager()
    assert cm.acquire_scope("task1", ["src/backend"]) is True
    assert cm.can_acquire_scope("task2", ["src/frontend"]) is True
    assert cm.can_acquire_scope("task3", ["src/backend/api"]) is False

    cm.release_scope("task1")
    assert cm.can_acquire_scope("task3", ["src/backend/api"]) is True


@pytest.mark.asyncio
async def test_dag_scheduler_dependencies():
    """Verify that dependent tasks wait for prerequisite tasks to finish."""
    pool = WorkerPool(max_parallel_agents=4)
    scheduler = TaskScheduler(pool)
    execution_order = []

    async def mock_work(task: Task):
        execution_order.append(task.task_id)
        await asyncio.sleep(0.05)
        return "ok"

    t1 = Task(task_id="t1", name="Task 1", description="")
    t2 = Task(task_id="t2", name="Task 2", description="")
    t3 = Task(
        task_id="t3",
        name="Task 3",
        description="",
        dependencies=["t1", "t2"],
        dependency_kind=DependencyKind.DEPENDENT,
    )

    task_map = await scheduler.run_plan([t1, t2, t3], mock_work)
    assert task_map["t1"].status == TaskState.COMPLETED
    assert task_map["t2"].status == TaskState.COMPLETED
    assert task_map["t3"].status == TaskState.COMPLETED

    # t1 and t2 should execute before t3
    assert execution_order.index("t3") > execution_order.index("t1")
    assert execution_order.index("t3") > execution_order.index("t2")


@pytest.mark.asyncio
async def test_failure_isolation():
    """Verify that a failing worker does not crash other independent tasks."""
    pool = WorkerPool(max_parallel_agents=4)
    scheduler = TaskScheduler(pool)

    async def mock_work(task: Task):
        if task.task_id == "failing_task":
            raise ValueError("Deliberate worker error")
        await asyncio.sleep(0.05)
        return "success"

    t1 = Task(task_id="t1", name="Independent Task 1", description="")
    t2 = Task(task_id="failing_task", name="Failing Task", description="")
    t3 = Task(task_id="t3", name="Independent Task 3", description="")

    task_map = await scheduler.run_plan([t1, t2, t3], mock_work)
    assert task_map["t1"].status == TaskState.COMPLETED
    assert task_map["failing_task"].status == TaskState.FAILED
    assert task_map["t3"].status == TaskState.COMPLETED


@pytest.mark.asyncio
async def test_master_agent_end_to_end_flow():
    """Verify full master agent orchestration including final reviewer pass."""
    master = AutonomousMasterAgent(max_parallel_agents=4, enable_ui=False)

    async def mock_work(task: Task):
        await asyncio.sleep(0.02)
        if task.role == AgentRole.REVIEWER:
            return "ALL_CHECKS_PASSED"
        return f"Processed {task.name}"

    tasks = [
        Task(
            task_id="db", name="Inspect DB", description="", role=AgentRole.RESEARCHER
        ),
        Task(
            task_id="api", name="Inspect API", description="", role=AgentRole.RESEARCHER
        ),
        Task(
            task_id="ui", name="Inspect UI", description="", role=AgentRole.RESEARCHER
        ),
        Task(
            task_id="integrate",
            name="Integration",
            description="",
            dependencies=["db", "api", "ui"],
            role=AgentRole.CODER,
        ),
    ]

    result = await master.run_plan("Build Module", tasks, mock_work)
    assert result.success is True
    assert result.reviewer_verdict == "ALL_CHECKS_PASSED"
    assert len(result.tasks) == 5  # 4 initial + 1 reviewer
