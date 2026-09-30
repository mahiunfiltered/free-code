"""Free Claude Code Parallel Task Orchestrator & Autonomous Agent Runtime."""

from .conflict_manager import ConflictManager
from .master_agent import AutonomousMasterAgent, WorkflowResult
from .models import (
    AgentRole,
    DependencyKind,
    ModelRole,
    Task,
    TaskState,
)
from .scheduler import TaskScheduler
from .ui import ProgressDashboard
from .worker_pool import WorkerPool

__all__ = [
    "AgentRole",
    "AutonomousMasterAgent",
    "ConflictManager",
    "DependencyKind",
    "ModelRole",
    "ProgressDashboard",
    "Task",
    "TaskScheduler",
    "TaskState",
    "WorkerPool",
    "WorkflowResult",
]
