from __future__ import annotations

"""High-autonomy Master Agent orchestrator with bounded execution and service isolation."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from free_claude_code.core.recovery import BoundedRecoveryEngine
from free_claude_code.core.shell_resolver import run_shell_command
from free_claude_code.core.watchdog import AntiStagnationWatchdog
from free_claude_code.orchestrator.conflict_manager import ConflictManager
from free_claude_code.orchestrator.models import (
    AgentRole,
    DependencyKind,
    ModelRole,
    Task,
    TaskState,
)
from free_claude_code.orchestrator.scheduler import TaskScheduler
from free_claude_code.orchestrator.ui import ProgressDashboard
from free_claude_code.orchestrator.worker_pool import WorkerPool
from free_claude_code.runtime.service_manager import get_service_manager


@dataclass
class WorkflowResult:
    goal: str
    success: bool
    duration_seconds: float
    tasks: list[Task]
    summary: str
    reviewer_verdict: str


class AutonomousMasterAgent:
    """Master agent coordinating parallel sub-agent workers for end-to-end tasks."""

    def __init__(
        self,
        max_parallel_agents: int = 4,
        model_name: str = "nvidia/nemotron-3-super-120b-a12b",
        enable_ui: bool = True,
        inactivity_timeout_seconds: float = 30.0,
    ) -> None:
        self.model_name = model_name
        self.enable_ui = enable_ui
        self.conflict_manager = ConflictManager()
        self.watchdog = AntiStagnationWatchdog(inactivity_timeout_seconds=inactivity_timeout_seconds)
        self.worker_pool = WorkerPool(
            max_parallel_agents=max_parallel_agents,
            conflict_manager=self.conflict_manager,
            watchdog=self.watchdog,
        )
        self.scheduler = TaskScheduler(self.worker_pool)
        self.dashboard = ProgressDashboard(title="Claude Code Autonomous Orchestrator")
        self.recovery_engine = BoundedRecoveryEngine(max_attempts=3)
        self.service_manager = get_service_manager()

    def _render_progress(self, master_state: str, tasks: list[Task]) -> None:
        if self.enable_ui:
            self.dashboard.render(
                master_state=master_state,
                tasks=tasks,
                model_name=self.model_name,
            )

    async def run_plan(
        self,
        goal: str,
        tasks: list[Task],
        task_executor_fn: Callable[[Task], Coroutine[Any, Any, Any]],
        total_timeout_seconds: float = 300.0,
    ) -> WorkflowResult:
        """Executes a defined goal plan with parallel sub-agents and final review."""
        start_time = time.time()
        self.watchdog.start()

        try:
            self._render_progress("PLANNING", tasks)

            # 1. Execute parallel task plan via scheduler
            def _on_update(t: Task):
                self._render_progress("RUNNING", tasks)

            await self.scheduler.run_plan(
                tasks=tasks,
                task_executor_fn=task_executor_fn,
                on_task_update=_on_update,
                total_timeout_seconds=total_timeout_seconds,
            )

            # 2. Check task execution outcomes
            completed = [t for t in tasks if t.status == TaskState.COMPLETED]
            failed = [t for t in tasks if t.status in (TaskState.FAILED, TaskState.BLOCKED)]
            success = len(failed) == 0

            # 3. Reviewer phase
            self._render_progress("REVIEWING", tasks)
            review_task = Task(
                task_id="task-reviewer-final",
                name="Final Quality Review",
                description=f"Review deliverables for goal: {goal}",
                role=AgentRole.REVIEWER,
                model_role=ModelRole.REVIEW_MODEL,
            )
            tasks.append(review_task)

            review_task_result = await self.worker_pool.execute_task(
                task=review_task,
                task_coro_fn=task_executor_fn,
            )

            total_duration = time.time() - start_time
            final_state = "COMPLETED" if success else "NEEDS_ATTENTION"
            self._render_progress(final_state, tasks)

            return WorkflowResult(
                goal=goal,
                success=success,
                duration_seconds=total_duration,
                tasks=tasks,
                summary=f"Executed {len(tasks)} tasks ({len(completed)} succeeded, {len(failed)} failed) in {total_duration:.2f}s",
                reviewer_verdict=str(review_task_result.result or "PASSED"),
            )
        finally:
            self.watchdog.stop()
