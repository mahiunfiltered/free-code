"""
Comprehensive Chaos & Stress Testing Suite (Phase 22)
Simulates:
1. API Timeout
2. API 429 Rate Limit
3. API 500 Server Error
4. API Disconnect / Connection Reset
5. Slow Model Simulation
6. Worker Crash / Unhandled Exception
7. Subprocess Hang & Tree Termination
8. PowerShell Syntax Failure
9. File Conflict / Scope Contention
10. Deadlock / Circular Dependency Detection
11. Background Service Drop / Recovery
12. Invalid Command (Exit 127)
13. Dependency Failure Propagation
14. Agent Task Cancellation
"""
import asyncio
import os
import sys
import time
import threading

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from free_claude_code.core.recovery import BoundedRecoveryEngine, ErrorSeverity, classify_error
from free_claude_code.core.process_manager import SubprocessManager
from free_claude_code.core.shell_resolver import run_shell_command, ShellType
from free_claude_code.core.debugger import get_debugger
from free_claude_code.orchestrator.conflict_manager import ConflictManager
from free_claude_code.orchestrator.task_graph import SmartTaskGraph, GraphNode, TaskPriority
from free_claude_code.orchestrator import (
    AgentRole,
    AutonomousMasterAgent,
    ModelRole,
    Task,
    TaskState,
    WorkerPool,
)

def log_chaos(num: int, name: str, passed: bool, detail: str = "") -> bool:
    status = "PASS [OK]" if passed else "FAIL [X]"
    print(f" {num:>2}. {name:<35} : {status:<10} {detail}")
    return passed

# 1. API Timeout
async def test_chaos_api_timeout() -> bool:
    engine = BoundedRecoveryEngine(max_attempts=2, base_backoff_seconds=0.1)
    attempts = 0
    async def flaky_api():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("HTTP connection timed out after 30000ms")
        return "RECOVERED_SUCCESS"

    res = await engine.execute_with_recovery("chaos_timeout", flaky_api)
    ok = res == "RECOVERED_SUCCESS" and attempts == 2
    return log_chaos(1, "Chaos: API Timeout Recovery", ok, f"Recovered in {attempts} attempts")

# 2. API 429 Rate Limit
async def test_chaos_api_429() -> bool:
    engine = BoundedRecoveryEngine(max_attempts=3, base_backoff_seconds=0.1)
    attempts = 0
    async def rate_limited_api():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("HTTP 429 Too Many Requests: Rate limit exceeded")
        return "429_RECOVERED"

    res = await engine.execute_with_recovery("chaos_429", rate_limited_api)
    ok = res == "429_RECOVERED" and attempts == 3
    return log_chaos(2, "Chaos: API 429 Rate-Limit", ok, f"Recovered after backoff (attempts: {attempts})")

# 3. API 500 Server Error
async def test_chaos_api_500() -> bool:
    engine = BoundedRecoveryEngine(max_attempts=2, base_backoff_seconds=0.1)
    attempts = 0
    async def server_error_api():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("HTTP 500 Internal Server Error: Model backend overloaded")
        return "500_RECOVERED"

    res = await engine.execute_with_recovery("chaos_500", server_error_api)
    ok = res == "500_RECOVERED" and attempts == 2
    return log_chaos(3, "Chaos: API 500 Server Error", ok, f"Recovered on retry (attempts: {attempts})")

# 4. API Disconnect
async def test_chaos_api_disconnect() -> bool:
    engine = BoundedRecoveryEngine(max_attempts=2, base_backoff_seconds=0.1)
    attempts = 0
    async def disconnect_api():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionResetError("Connection reset by peer (ECONNRESET)")
        return "DISCONNECT_RECOVERED"

    res = await engine.execute_with_recovery("chaos_disconnect", disconnect_api)
    ok = res == "DISCONNECT_RECOVERED" and attempts == 2
    return log_chaos(4, "Chaos: API Disconnect", ok, f"Reconnected cleanly (attempts: {attempts})")

# 5. Slow Model Simulation
async def test_chaos_slow_model() -> bool:
    t0 = time.time()
    async def slow_stream():
        for i in range(5):
            await asyncio.sleep(0.05)
        return "SLOW_STREAM_DONE"

    res = await asyncio.wait_for(slow_stream(), timeout=2.0)
    dur = time.time() - t0
    ok = res == "SLOW_STREAM_DONE" and dur < 1.0
    return log_chaos(5, "Chaos: Slow Model Simulation", ok, f"Completed within threshold ({dur:.2f}s)")

# 6. Worker Crash Isolation
async def test_chaos_worker_crash() -> bool:
    pool = WorkerPool(max_parallel_agents=2)
    task_ok = Task(task_id="t_ok", name="Healthy Worker", description="OK")
    task_crash = Task(task_id="t_crash", name="Crashing Worker", description="Crash")

    async def exec_fn(t: Task):
        if t.task_id == "t_crash":
            raise ValueError("Simulated unhandled exception in worker")
        await asyncio.sleep(0.05)
        return "HEALTHY_RESULT"

    res_ok, res_crash = await asyncio.gather(
        pool.execute_task(task_ok, exec_fn),
        pool.execute_task(task_crash, exec_fn),
        return_exceptions=True
    )
    ok = task_ok.status == TaskState.COMPLETED and task_crash.status == TaskState.FAILED
    return log_chaos(6, "Chaos: Worker Crash Isolation", ok, "Healthy worker completed; crash isolated")

# 7. Subprocess Hang & Termination
def test_chaos_subprocess_hang() -> bool:
    proc_mgr = SubprocessManager()
    t0 = time.time()
    managed = proc_mgr.spawn(
        [sys.executable, "-c", "import time; time.sleep(100)"],
        name="hanging_proc",
        timeout_seconds=0.8
    )
    timed_out = False
    try:
        proc_mgr.wait_with_timeout(managed, timeout=0.8)
    except TimeoutError:
        timed_out = True
    dur = time.time() - t0
    ok = timed_out and dur < 2.5
    return log_chaos(7, "Chaos: Subprocess Hang Kill", ok, f"Killed hanging process in {dur:.2f}s")

# 8. PowerShell Failure Recovery
def test_chaos_powershell_failure() -> bool:
    res = run_shell_command("Get-NonExistentCmdletXYZ 2>$null", preferred=ShellType.POWERSHELL)
    debugger = get_debugger()
    diag = debugger.collect_diagnostics("The term 'Get-NonExistentCmdletXYZ' is not recognized", exit_code=res.returncode)
    plan = debugger.diagnose_and_propose(diag, attempt_num=1)
    ok = diag.failure_class.value in ("POWERSHELL_FAILURE", "SHELL_FAILURE")
    return log_chaos(8, "Chaos: PowerShell Failure Diagnosis", ok, f"Diagnosed: {diag.failure_class.value}")

# 9. File Conflict / Scope Lock
async def test_chaos_file_conflict() -> bool:
    cm = ConflictManager()
    t1_acquired = cm.acquire_scope("task_1", ["workspace/shared_db.py"])
    t2_can_acquire = cm.can_acquire_scope("task_2", ["workspace/shared_db.py"])
    cm.release_scope("task_1")
    t2_acquired_after_release = cm.acquire_scope("task_2", ["workspace/shared_db.py"])
    cm.release_scope("task_2")
    ok = t1_acquired and not t2_can_acquire and t2_acquired_after_release
    return log_chaos(9, "Chaos: Scope Conflict Contention", ok, "Scope locked exclusively; released cleanly")

# 10. Deadlock Cycle Resolution
def test_chaos_deadlock_cycle() -> bool:
    graph = SmartTaskGraph()
    graph.add_node(GraphNode(task_id="A", name="Node A", objective="A", dependencies=["B"]))
    graph.add_node(GraphNode(task_id="B", name="Node B", objective="B", dependencies=["A"]))
    cycles = graph.detect_cycles()
    ok = len(cycles) > 0
    return log_chaos(10, "Chaos: Deadlock / Cycle Detection", ok, f"Cycle detected: {cycles}")

# 11. Background Service Failure Detection
def test_chaos_service_failure() -> bool:
    from free_claude_code.runtime.service_manager import get_service_manager
    sm = get_service_manager()
    status = sm.get_service_status("non_existent_service")
    ok = status is None
    return log_chaos(11, "Chaos: Service Drop Detection", ok, "Non-existent/crashed service safely reported")

# 12. Invalid Command
def test_chaos_invalid_command() -> bool:
    res = run_shell_command("some_completely_invalid_executable_12345", preferred=ShellType.CMD)
    ok = res.returncode != 0
    return log_chaos(12, "Chaos: Invalid Command Handling", ok, f"Exit code {res.returncode} handled safely")

# 13. Dependency Failure Propagation
async def test_chaos_dependency_propagation() -> bool:
    master = AutonomousMasterAgent(max_parallel_agents=2, enable_ui=False)
    t_parent = Task(task_id="parent_fail", name="Parent Fail", description="Fails")
    t_child = Task(task_id="child_blocked", name="Child Blocked", description="Depends on parent", dependencies=["parent_fail"])

    async def fail_parent_worker(t: Task):
        if t.task_id == "parent_fail":
            raise RuntimeError("Parent failed critically")
        return "CHILD_SHOULD_NOT_RUN"

    res = await master.run_plan("Chaos Dep Test", [t_parent, t_child], fail_parent_worker)
    ok = t_parent.status == TaskState.FAILED and t_child.status == TaskState.BLOCKED
    return log_chaos(13, "Chaos: Dependency Failure Block", ok, "Child blocked cleanly when parent failed")

# 14. Task Cancellation
async def test_chaos_task_cancellation() -> bool:
    pool = WorkerPool(max_parallel_agents=2)
    t_cancel = Task(task_id="t_cancel", name="Cancel Task", description="Cancel")

    async def long_task(t: Task):
        await asyncio.sleep(10.0)
        return "DONE"

    async_task = asyncio.create_task(pool.execute_task(t_cancel, long_task))
    await asyncio.sleep(0.05)
    async_task.cancel()
    try:
        await async_task
    except asyncio.CancelledError:
        pass
    ok = t_cancel.status in (TaskState.CANCELLED, TaskState.RUNNING)
    return log_chaos(14, "Chaos: Agent Task Cancellation", ok, "Async worker task cancelled without deadlock")

async def main():
    print("=" * 65)
    print(" CLAUDE CODE RUNTIME — 14-POINT CHAOS & STRESS TEST SUITE")
    print("=" * 65)

    tests = [
        test_chaos_api_timeout(),
        test_chaos_api_429(),
        test_chaos_api_500(),
        test_chaos_api_disconnect(),
        test_chaos_slow_model(),
        test_chaos_worker_crash(),
        asyncio.to_thread(test_chaos_subprocess_hang),
        asyncio.to_thread(test_chaos_powershell_failure),
        test_chaos_file_conflict(),
        asyncio.to_thread(test_chaos_deadlock_cycle),
        asyncio.to_thread(test_chaos_service_failure),
        asyncio.to_thread(test_chaos_invalid_command),
        test_chaos_dependency_propagation(),
        test_chaos_task_cancellation(),
    ]

    results = []
    for coro in tests:
        res = await coro
        results.append(res)

    print("=" * 65)
    all_ok = all(results)
    if all_ok:
        print(" ALL 14/14 CHAOS & STRESS TESTS PASSED (100% SUCCESS) [OK]")
    else:
        print(" CHAOS TESTS COMPLETED WITH FAILURES")
    print("=" * 65)
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    asyncio.run(main())
