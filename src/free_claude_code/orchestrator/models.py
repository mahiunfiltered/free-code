"""Data models for parallel agent task orchestration."""

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    RETRYING = "RETRYING"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    REVIEWING = "REVIEWING"


class DependencyKind(StrEnum):
    INDEPENDENT = "INDEPENDENT"
    DEPENDENT = "DEPENDENT"
    BLOCKING = "BLOCKING"
    OPTIONAL = "OPTIONAL"
    FINAL_REVIEW = "FINAL_REVIEW"


class TaskComplexity(StrEnum):
    TRIVIAL = "TRIVIAL"  # Single agent, minimal context/tools
    SMALL = "SMALL"  # 1-2 workers, fast path
    MEDIUM = "MEDIUM"  # Parallel specialized agents
    LARGE = "LARGE"  # Full DAG orchestration
    ENTERPRISE = "ENTERPRISE"  # Multi-stage parallel DAG with validation stages


class AgentRole(StrEnum):
    # Specialized Phase 4 Roles
    ARCHITECT = "ARCHITECT"
    BACKEND = "BACKEND"
    FRONTEND = "FRONTEND"
    DATABASE = "DATABASE"
    AUTH = "AUTH"
    DATA = "DATA"
    TEST = "TEST"
    DEBUGGER = "DEBUGGER"
    SECURITY = "SECURITY"
    PERFORMANCE = "PERFORMANCE"
    REVIEWER = "REVIEWER"
    INTEGRATION = "INTEGRATION"
    # Core & Backward Compatible Roles
    MASTER = "MASTER"
    PLANNER = "PLANNER"
    RESEARCHER = "RESEARCHER"
    CODER = "CODER"
    TESTER = "TEST"


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
    complexity: TaskComplexity = TaskComplexity.MEDIUM
    scope_paths: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    dependency_kind: DependencyKind = DependencyKind.INDEPENDENT
    status: TaskState = TaskState.CREATED
    start_time: float | None = None
    end_time: float | None = None
    result: Any = None
    error: str | None = None
    retry_count: int = 0
    # Observability & Audit Fields (Phase 19)
    parent_task: str | None = None
    command: str | None = None
    exit_code: int | None = None
    model: str | None = None
    provider: str | None = None
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if self.start_time is None:
            return 0.0
        end = self.end_time or time.time()
        return end - self.start_time
