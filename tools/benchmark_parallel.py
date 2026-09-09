import asyncio
import os
import sys
import time

# Ensure UTF-8 output encoding for Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from free_claude_code.orchestrator import (
    AutonomousMasterAgent,
    AgentRole,
    ModelRole,
    Task,
    TaskState,
    WorkerPool,
)

async def simulate_independent_engineering_task(task: Task):
    """Performs real filesystem analysis and workload on distinct repository components."""
    # Analyze files in scope
    total_lines = 0
    total_files = 0
    if task.scope_paths:
        for p in task.scope_paths:
            if os.path.exists(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        if f.endswith((".py", ".json", ".js", ".md", ".ts")):
                            total_files += 1
                            try:
                                with open(os.path.join(root, f), "r", encoding="utf-8", errors="ignore") as fp:
                                    total_lines += len(fp.readlines())
                            except Exception:
                                pass

    # Simulate realistic LLM analysis / compute duration (0.5s per task)
    await asyncio.sleep(0.5)
    return {
        "task_id": task.task_id,
        "files_scanned": total_files,
        "lines_scanned": total_lines,
        "status": "ANALYSIS_COMPLETE"
    }

async def run_sequential_benchmark(tasks: list[Task]) -> float:
    start_time = time.time()
    results = []
    for t in tasks:
        t.status = TaskState.RUNNING
        t.start_time = time.time()
        res = await simulate_independent_engineering_task(t)
        t.result = res
        t.status = TaskState.COMPLETED
        t.end_time = time.time()
        results.append(t)
    duration = time.time() - start_time
    return duration

async def run_parallel_benchmark(tasks: list[Task], max_parallel: int = 4) -> float:
    start_time = time.time()
    pool = WorkerPool(max_parallel_agents=max_parallel)
    results = await asyncio.gather(*(
        pool.execute_task(t, simulate_independent_engineering_task)
        for t in tasks
    ))
    duration = time.time() - start_time
    return duration

def create_benchmark_tasks():
    return [
        Task(
            task_id="backend_analysis",
            name="Inspect Backend API",
            description="Scan API handlers and routing",
            role=AgentRole.RESEARCHER,
            scope_paths=["src/free_claude_code/api"],
        ),
        Task(
            task_id="providers_analysis",
            name="Inspect Model Providers",
            description="Scan provider adapters and configurations",
            role=AgentRole.RESEARCHER,
            scope_paths=["src/free_claude_code/providers"],
        ),
        Task(
            task_id="config_analysis",
            name="Inspect Configuration",
            description="Scan settings and catalogs",
            role=AgentRole.RESEARCHER,
            scope_paths=["src/free_claude_code/config"],
        ),
        Task(
            task_id="cli_analysis",
            name="Inspect CLI Launchers",
            description="Scan CLI entrypoints and inputs",
            role=AgentRole.RESEARCHER,
            scope_paths=["src/free_claude_code/cli"],
        ),
    ]

async def main():
    print("=" * 65)
    print(" RUNNING SEQUENTIAL VS PARALLEL TASK BENCHMARK")
    print("=" * 65)

    # 1. Sequential Run
    seq_tasks = create_benchmark_tasks()
    print("Executing 4 independent tasks SEQUENTIALLY...")
    seq_duration = await run_sequential_benchmark(seq_tasks)
    print(f"Sequential Execution Time: {seq_duration:.3f}s")

    # 2. Parallel Run
    par_tasks = create_benchmark_tasks()
    print("Executing 4 independent tasks IN PARALLEL (Max Workers = 4)...")
    par_duration = await run_parallel_benchmark(par_tasks, max_parallel=4)
    print(f"Parallel Execution Time:   {par_duration:.3f}s")

    speedup = seq_duration / par_duration if par_duration > 0 else 1.0
    improvement_pct = ((seq_duration - par_duration) / seq_duration) * 100.0 if seq_duration > 0 else 0.0

    print("=" * 65)
    print(f" SEQUENTIAL DURATION : {seq_duration:.3f}s")
    print(f" PARALLEL DURATION   : {par_duration:.3f}s")
    print(f" MEASURED SPEEDUP    : {speedup:.2f}x")
    print(f" MEASURED IMPROVEMENT: {improvement_pct:.1f}% reduction in wall-clock time")
    print("=" * 65)

if __name__ == "__main__":
    asyncio.run(main())
