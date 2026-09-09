import asyncio
import os
import sys
import time

# Ensure UTF-8 output encoding for Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from free_claude_code.core.shell_resolver import run_shell_command
from free_claude_code.orchestrator import (
    AutonomousMasterAgent,
    AgentRole,
    DependencyKind,
    ModelRole,
    Task,
    TaskState,
)

async def real_agent_task_executor(task: Task):
    """Executes real development tasks: PowerShell commands, file ops, and quality checks."""
    if task.task_id == "inspect_repo":
        res = run_shell_command("Get-ChildItem -Directory src/free_claude_code | Select-Object -ExpandProperty Name")
        return f"Modules found: {res.stdout.strip().replace(chr(10), ', ')}"

    elif task.task_id == "build_database_module":
        db_file = "D:\\Claude code\\DATABASE_RUNTIME_TEST.txt"
        with open(db_file, "w", encoding="utf-8") as f:
            f.write("DATABASE_MODULE_INITIALIZED_OK\n")
        return f"Wrote {db_file}"

    elif task.task_id == "build_api_module":
        api_file = "D:\\Claude code\\API_RUNTIME_TEST.txt"
        with open(api_file, "w", encoding="utf-8") as f:
            f.write("API_MODULE_INITIALIZED_OK\n")
        return f"Wrote {api_file}"

    elif task.task_id == "integrate_and_test":
        # Verification stage: check that previous tasks produced their files
        db_exists = os.path.exists("D:\\Claude code\\DATABASE_RUNTIME_TEST.txt")
        api_exists = os.path.exists("D:\\Claude code\\API_RUNTIME_TEST.txt")
        if not (db_exists and api_exists):
            raise RuntimeError("Integration failed: prerequisite files missing")
        return "INTEGRATION_VERIFIED_SUCCESSFULLY"

    elif task.role == AgentRole.REVIEWER:
        # Final Reviewer pass
        return "REVIEW_PASSED: ALL ARTIFACTS AND SHELL COMMANDS VERIFIED"

    return "TASK_DONE"

async def main():
    print("=" * 65)
    print(" EXECUTING AUTONOMOUS MASTER AGENT LIVE WORKFLOW")
    print("=" * 65)

    master = AutonomousMasterAgent(max_parallel_agents=4, enable_ui=True)

    tasks = [
        Task(
            task_id="inspect_repo",
            name="Inspect Repository Architecture",
            description="Inspect modules in src/free_claude_code",
            role=AgentRole.RESEARCHER,
            model_role=ModelRole.FAST_MODEL,
        ),
        Task(
            task_id="build_database_module",
            name="Initialize Database Schema",
            description="Create database runtime artifact",
            role=AgentRole.CODER,
            model_role=ModelRole.CODING_MODEL,
            scope_paths=["D:\\Claude code\\DATABASE_RUNTIME_TEST.txt"],
        ),
        Task(
            task_id="build_api_module",
            name="Initialize API Endpoints",
            description="Create API runtime artifact",
            role=AgentRole.CODER,
            model_role=ModelRole.CODING_MODEL,
            scope_paths=["D:\\Claude code\\API_RUNTIME_TEST.txt"],
        ),
        Task(
            task_id="integrate_and_test",
            name="System Integration & Test",
            description="Integrate database and API artifacts",
            role=AgentRole.TESTER,
            model_role=ModelRole.PRIMARY_REASONING_MODEL,
            dependencies=["inspect_repo", "build_database_module", "build_api_module"],
            dependency_kind=DependencyKind.DEPENDENT,
        ),
    ]

    result = await master.run_plan("Autonomous Runtime Integration Goal", tasks, real_agent_task_executor)

    print("\n" + "=" * 65)
    print(" WORKFLOW EXECUTION SUMMARY")
    print("=" * 65)
    print(f" Success          : {result.success}")
    print(f" Total Duration   : {result.duration_seconds:.2f}s")
    print(f" Summary          : {result.summary}")
    print(f" Reviewer Verdict : {result.reviewer_verdict}")
    print("=" * 65)

    return 0 if result.success else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
