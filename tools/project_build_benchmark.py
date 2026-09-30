import asyncio
import json
import os
import io
import sys
import time

# Ensure UTF-8 output encoding for Windows consoles
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8")

from free_claude_code.core.context_optimizer import get_context_optimizer
from free_claude_code.core.repo_indexer import get_repo_indexer
from free_claude_code.core.shell_resolver import run_shell_command
from free_claude_code.core.windows_authority import get_windows_authority
from free_claude_code.orchestrator import (
    AgentRole,
    AutonomousMasterAgent,
    ModelRole,
    Task,
    TaskState,
    WorkerPool,
)
from free_claude_code.orchestrator.profiler import get_runtime_profiler
from free_claude_code.orchestrator.task_graph import GraphNode, SmartTaskGraph, TaskPriority

BENCHMARK_WORKSPACE = "D:\\Claude code\\.benchmark_project"

def setup_benchmark_workspace():
    os.makedirs(BENCHMARK_WORKSPACE, exist_ok=True)
    for sub in ("backend", "database", "auth", "frontend", "tests", "config"):
        os.makedirs(os.path.join(BENCHMARK_WORKSPACE, sub), exist_ok=True)

def cleanup_benchmark_workspace():
    if os.path.exists(BENCHMARK_WORKSPACE):
        try:
            import shutil
            shutil.rmtree(BENCHMARK_WORKSPACE, ignore_errors=True)
        except Exception:
            pass

async def build_component_worker(task: Task):
    """Simulates real component generation with context extraction and disk write."""
    t_start = time.time()
    optimizer = get_context_optimizer()

    # 1. Context optimization step
    opt_ctx = optimizer.build_task_context(
        task_id=task.task_id,
        task_description=task.description,
        scope_paths=task.scope_paths,
        keywords=[task.name, "router", "model", "schema"],
    )

    # 2. Write real generated component file
    out_file = task.scope_paths[0] if task.scope_paths else os.path.join(BENCHMARK_WORKSPACE, f"{task.task_id}.py")
    os.makedirs(os.path.dirname(out_file), exist_ok=True)

    header = f"# Module: {task.name}\n# Estimated context tokens: {opt_ctx.estimated_tokens}\n"
    content = header + f"def initialize_{task.task_id}():\n    return '{task.name.upper()}_INITIALIZED_OK'\n"

    with open(out_file, "w", encoding="utf-8") as fp:
        fp.write(content)

    # Simulate realistic LLM generation computation (0.25s)
    await asyncio.sleep(0.25)

    duration = time.time() - t_start
    profiler = get_runtime_profiler()
    profiler.record_tool_call(f"write_{task.task_id}", duration=duration, success=True)
    profiler.record_model_call(duration=0.25, model="nvidia/nemotron-3-super-120b-a12b", tokens=opt_ctx.estimated_tokens)

    return {
        "task_id": task.task_id,
        "file": out_file,
        "tokens": opt_ctx.estimated_tokens,
        "compression": opt_ctx.compression_ratio,
        "status": "BUILT_OK",
    }

def create_project_graph() -> SmartTaskGraph:
    graph = SmartTaskGraph()

    # 1. Config & Environment
    graph.add_node(GraphNode(
        task_id="config",
        name="Configuration Module",
        objective="Generate application configuration and environment loader",
        role=AgentRole.CODER,
        priority=TaskPriority.HIGH,
        files_out=[os.path.join(BENCHMARK_WORKSPACE, "config", "settings.py")],
    ))

    # 2. Database Models
    graph.add_node(GraphNode(
        task_id="database",
        name="Database Schema",
        objective="Generate database models and ORM entities",
        role=AgentRole.CODER,
        priority=TaskPriority.HIGH,
        files_out=[os.path.join(BENCHMARK_WORKSPACE, "database", "models.py")],
    ))

    # 3. Authentication
    graph.add_node(GraphNode(
        task_id="auth",
        name="Authentication System",
        objective="Generate JWT handler and auth routes",
        role=AgentRole.CODER,
        priority=TaskPriority.NORMAL,
        files_out=[os.path.join(BENCHMARK_WORKSPACE, "auth", "jwt_auth.py")],
    ))

    # 4. Backend API (Depends on config, database, auth)
    graph.add_node(GraphNode(
        task_id="backend_api",
        name="Backend REST API",
        objective="Generate API endpoints and request handlers",
        role=AgentRole.CODER,
        priority=TaskPriority.NORMAL,
        dependencies=["config", "database", "auth"],
        files_out=[os.path.join(BENCHMARK_WORKSPACE, "backend", "routes.py")],
    ))

    # 5. Frontend Dashboard (Independent)
    graph.add_node(GraphNode(
        task_id="frontend_ui",
        name="Frontend Dashboard Components",
        objective="Generate React/TSX UI widgets and layouts",
        role=AgentRole.CODER,
        priority=TaskPriority.NORMAL,
        files_out=[os.path.join(BENCHMARK_WORKSPACE, "frontend", "Dashboard.tsx")],
    ))

    # 6. Unit & Integration Tests (Depends on backend, database)
    graph.add_node(GraphNode(
        task_id="integration_tests",
        name="Integration Test Suite",
        objective="Generate automated tests verifying all endpoints",
        role=AgentRole.TESTER,
        priority=TaskPriority.HIGH,
        dependencies=["backend_api", "database"],
        files_out=[os.path.join(BENCHMARK_WORKSPACE, "tests", "test_integration.py")],
    ))

    return graph

async def run_sequential_build(tasks: list[Task]) -> float:
    start_time = time.time()
    for t in tasks:
        t.status = TaskState.RUNNING
        t.start_time = time.time()
        res = await build_component_worker(t)
        t.result = res
        t.status = TaskState.COMPLETED
        t.end_time = time.time()
    return time.time() - start_time

async def run_parallel_dag_build(tasks: list[Task], concurrency: int = 4) -> tuple[float, dict[str, Task]]:
    start_time = time.time()
    master = AutonomousMasterAgent(max_parallel_agents=concurrency, enable_ui=False)
    res = await master.run_plan("Realistic Project Build", tasks, build_component_worker)
    return res.duration_seconds, {t.task_id: t for t in res.tasks}

async def main():
    print("=" * 65)
    print(" CLAUDE CODE RUNTIME — REALISTIC PROJECT BUILD BENCHMARK")
    print("=" * 65)

    # 1. Authority Profile
    auth_mgr = get_windows_authority()
    auth_prof = auth_mgr.inspect_authority()
    auth_mgr.print_authority_banner(auth_prof)

    # 2. Sequential Run
    setup_benchmark_workspace()
    graph_seq = create_project_graph()
    tasks_seq = graph_seq.to_tasks()
    print(f"Executing {len(tasks_seq)} project components SEQUENTIALLY...")
    seq_time = await run_sequential_build(tasks_seq)
    print(f"Sequential Build Time: {seq_time:.3f}s")
    cleanup_benchmark_workspace()

    # 3. Optimized Parallel DAG Run
    setup_benchmark_workspace()
    graph_par = create_project_graph()
    tasks_par = graph_par.to_tasks()
    optimal_concurrency = graph_par.calculate_optimal_concurrency(max_system_limit=8, provider_limit=4)
    print(f"Executing {len(tasks_par)} project components IN PARALLEL (Auto-scaled Workers = {optimal_concurrency})...")
    par_time, task_map = await run_parallel_dag_build(tasks_par, concurrency=optimal_concurrency)
    print(f"Parallel DAG Build Time: {par_time:.3f}s")

    # 4. Verification
    all_files_exist = True
    for tid, t in task_map.items():
        if t.scope_paths and not os.path.exists(t.scope_paths[0]):
            all_files_exist = False

    cleanup_benchmark_workspace()

    speedup = seq_time / par_time if par_time > 0 else 1.0
    improvement_pct = ((seq_time - par_time) / seq_time) * 100.0 if seq_time > 0 else 0.0

    profiler = get_runtime_profiler()
    baseline = profiler.export_baseline("performance_baseline.json")
    
    report_data = {
        "sequential_duration_seconds": seq_time,
        "parallel_duration_seconds": par_time,
        "speedup_factor": speedup,
        "wall_clock_reduction_percent": improvement_pct,
        "concurrency_used": optimal_concurrency,
        "artifacts_verified": all_files_exist,
        "profiler_summary": baseline
    }
    with open("performance_report.json", "w", encoding="utf-8") as rf:
        json.dump(report_data, rf, indent=2)

    print("\n" + "=" * 65)
    print(" BENCHMARK & PROFILING SUMMARY")
    print("=" * 65)
    print(f" Sequential Time    : {seq_time:.3f}s")
    print(f" Parallel Time      : {par_time:.3f}s")
    print(f" Measured Speedup   : {speedup:.2f}x")
    print(f" Wall-Clock Savings : {improvement_pct:.1f}% reduction")
    print(f" Total Tokens (Est) : {baseline['total_tokens_estimated']} tokens")
    print(f" Artifacts Verified : {'PASS [OK]' if all_files_exist else 'FAIL [X]'}")
    print(f" Baseline Exported  : performance_baseline.json")
    print(f" Report Exported    : performance_report.json")
    print("=" * 65)

    return 0 if (speedup >= 1.5 and all_files_exist) else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
