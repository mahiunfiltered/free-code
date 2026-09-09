from __future__ import annotations

"""Dependency-aware DAG task scheduler with deadlock detection and bounded timeouts."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any

from free_claude_code.orchestrator.models import DependencyKind, Task, TaskState
from free_claude_code.orchestrator.worker_pool import WorkerPool


class TaskScheduler:
    """Schedules tasks respecting dependency graphs, concurrency limits, and deadlock prevention."""

    def __init__(self, worker_pool: WorkerPool) -> None:
        self.worker_pool = worker_pool

    async def run_plan(
        self,
        tasks: list[Task],
        task_executor_fn: Callable[[Task], Coroutine[Any, Any, Any]],
        on_task_update: Callable[[Task], None] | None = None,
        total_timeout_seconds: float = 300.0,
    ) -> dict[str, Task]:
        """Executes a list of tasks respecting dependencies, concurrency limits, and timeouts."""
        start_time = time.time()
        task_map: dict[str, Task] = {t.task_id: t for t in tasks}
        completed_task_ids: set[str] = set()
        failed_task_ids: set[str] = set()
        active_futures: dict[str, asyncio.Task] = {}

        async def _run_worker(t: Task):
            res_task = await self.worker_pool.execute_task(t, task_executor_fn)
            if on_task_update:
                on_task_update(res_task)
            return res_task

        while len(completed_task_ids) + len(failed_task_ids) < len(tasks):
            # Enforce total plan timeout
            if time.time() - start_time > total_timeout_seconds:
                for t in tasks:
                    if t.status in (TaskState.CREATED, TaskState.QUEUED, TaskState.WAITING, TaskState.RUNNING):
                        t.status = TaskState.FAILED
                        t.error = f"Overall plan exceeded timeout of {total_timeout_seconds}s"
                break

            # Find tasks that are ready to run
            ready_tasks: list[Task] = []
            for t in tasks:
                if t.task_id in active_futures or t.task_id in completed_task_ids or t.task_id in failed_task_ids:
                    continue

                # Check dependencies
                deps_met = True
                dep_failed = False
                for dep_id in t.dependencies:
                    if dep_id in failed_task_ids:
                        dep_failed = True
                        break
                    if dep_id not in completed_task_ids:
                        deps_met = False
                        break

                if dep_failed:
                    if t.dependency_kind == DependencyKind.OPTIONAL:
                        ready_tasks.append(t)
                    else:
                        t.status = TaskState.BLOCKED
                        t.error = f"Prerequisite dependency failed"
                        failed_task_ids.add(t.task_id)
                        if on_task_update:
                            on_task_update(t)
                elif deps_met:
                    ready_tasks.append(t)

            # Launch ready tasks
            for t in ready_tasks:
                fut = asyncio.create_task(_run_worker(t))
                active_futures[t.task_id] = fut

            # Deadlock detection: No active tasks and no ready tasks while uncompleted tasks remain
            if not active_futures:
                remaining = [
                    t for t in tasks
                    if t.task_id not in completed_task_ids and t.task_id not in failed_task_ids
                ]
                for rem_task in remaining:
                    rem_task.status = TaskState.BLOCKED
                    rem_task.error = f"Deadlock detected: unresolved dependencies {rem_task.dependencies}"
                    failed_task_ids.add(rem_task.task_id)
                    if on_task_update:
                        on_task_update(rem_task)
                break

            # Wait for at least one active task to complete with bounded iteration timeout
            done, _ = await asyncio.wait(
                active_futures.values(),
                timeout=10.0,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Process completed tasks
            for fut in done:
                res_task: Task = await fut
                active_futures.pop(res_task.task_id, None)
                if res_task.status == TaskState.COMPLETED:
                    completed_task_ids.add(res_task.task_id)
                else:
                    failed_task_ids.add(res_task.task_id)

        return task_map
