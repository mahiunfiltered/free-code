from __future__ import annotations

"""Sub-agent worker pool for concurrent task execution."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from free_claude_code.core.recovery import BoundedRecoveryEngine
from free_claude_code.core.watchdog import ActivityKind, AntiStagnationWatchdog
from free_claude_code.orchestrator.conflict_manager import ConflictManager
from free_claude_code.orchestrator.models import AgentRole, Task, TaskState


class WorkerPool:
    """Manages concurrent worker execution with resource control and failure isolation."""

    def __init__(
        self,
        max_parallel_agents: int = 4,
        conflict_manager: ConflictManager | None = None,
        watchdog: AntiStagnationWatchdog | None = None,
    ) -> None:
        self.max_parallel_agents = max_parallel_agents
        self.conflict_manager = conflict_manager or ConflictManager()
        self.watchdog = watchdog or AntiStagnationWatchdog()
        self.recovery_engine = BoundedRecoveryEngine(max_attempts=3)
        self._semaphore = asyncio.Semaphore(max_parallel_agents)
        self._active_tasks: dict[str, Task] = {}
        self._tasks_lock = asyncio.Lock()

    async def execute_task(
        self,
        task: Task,
        task_coro_fn: Callable[[Task], Coroutine[Any, Any, Any]],
        timeout_seconds: float = 120.0,
    ) -> Task:
        """Executes a single task within the worker pool with concurrency, timeout, and conflict locks."""
        task.status = TaskState.QUEUED
        async with self._tasks_lock:
            self._active_tasks[task.task_id] = task

        # Wait for file/path scope lock if scope paths are specified
        while task.scope_paths and not self.conflict_manager.can_acquire_scope(task.task_id, task.scope_paths):
            task.status = TaskState.WAITING
            await asyncio.sleep(0.5)

        if task.scope_paths:
            self.conflict_manager.acquire_scope(task.task_id, task.scope_paths)

        async with self._semaphore:
            task.status = TaskState.RUNNING
            task.start_time = time.time()
            act = self.watchdog.register_activity(
                activity_id=task.task_id,
                kind=ActivityKind.WORKER_TASK,
                description=f"{task.role.value}: {task.name}",
                metadata={"role": task.role.value, "model_role": task.model_role.value},
            )

            try:
                # Execute with timeout and bounded recovery
                async def _run():
                    res = await asyncio.wait_for(task_coro_fn(task), timeout=timeout_seconds)
                    return res

                result = await self.recovery_engine.execute_with_recovery(
                    task_id=task.task_id,
                    coro_fn=_run,
                )
                task.result = result
                task.status = TaskState.COMPLETED
                task.end_time = time.time()
            except asyncio.TimeoutError:
                task.status = TaskState.FAILED
                task.error = f"Task timed out after {timeout_seconds}s"
                task.end_time = time.time()
            except Exception as exc:
                task.status = TaskState.FAILED
                task.error = str(exc)
                task.end_time = time.time()
            finally:
                self.watchdog.finish_activity(task.task_id)
                if task.scope_paths:
                    self.conflict_manager.release_scope(task.task_id)

        return task

    async def get_active_tasks(self) -> list[Task]:
        async with self._tasks_lock:
            return list(self._active_tasks.values())
