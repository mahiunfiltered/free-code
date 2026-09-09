from __future__ import annotations

"""Data models for parallel agent task orchestration."""

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    REVIEWING = "REVIEWING"


class DependencyKind(StrEnum):
    INDEPENDENT = "INDEPENDENT"
    DEPENDENT = "DEPENDENT"
    BLOCKING = "BLOCKING"
    OPTIONAL = "OPTIONAL"
    FINAL_REVIEW = "FINAL_REVIEW"


class AgentRole(StrEnum):
    MASTER = "MASTER"
    PLANNER = "PLANNER"
    RESEARCHER = "RESEARCHER"
    CODER = "CODER"
    DEBUGGER = "DEBUGGER"
    TESTER = "TESTER"
    REVIEWER = "REVIEWER"


class ModelRole(StrEnum):
    PRIMARY_REASONING_MODEL = "PRIMARY_REASONING_MODEL"
    FAST_MODEL = "FAST_MODEL"
    CODING_MODEL = "CODING_MODEL"
    REVIEW_MODEL = "REVIEW_MODEL"
    SMALL_TASK_MODEL = "SMALL_TASK_MODEL"
    FALLBACK_MODEL = "FALLBACK_MODEL"


@dataclass
class Task:
    task_id: str
    name: str
    description: str
    role: AgentRole = AgentRole.CODER
    model_role: ModelRole = ModelRole.CODING_MODEL
    scope_paths: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    dependency_kind: DependencyKind = DependencyKind.INDEPENDENT
    status: TaskState = TaskState.CREATED
    start_time: float | None = None
    end_time: float | None = None
    result: Any = None
    error: str | None = None
    retry_count: int = 0

    @property
    def duration_seconds(self) -> float:
        if self.start_time is None:
            return 0.0
        end = self.end_time or time.time()
        return end - self.start_time
