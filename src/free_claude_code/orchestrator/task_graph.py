from __future__ import annotations

"""Smart task graph and dependency DAG with dynamic worker auto-scaling."""

import os
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Sequence

from free_claude_code.orchestrator.models import (
    AgentRole,
    DependencyKind,
    ModelRole,
    Task,
    TaskState,
)


class TaskPriority(IntEnum):
    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class GraphNode:
    task_id: str
    name: str
    objective: str
    role: AgentRole = AgentRole.CODER
    model_role: ModelRole = ModelRole.CODING_MODEL
    priority: TaskPriority = TaskPriority.NORMAL
    dependencies: list[str] = field(default_factory=list)
    files_in: list[str] = field(default_factory=list)
    files_out: list[str] = field(default_factory=list)
    timeout_seconds: float = 60.0
    estimated_cost_tokens: int = 500

    def to_task(self) -> Task:
        return Task(
            task_id=self.task_id,
            name=self.name,
            description=self.objective,
            role=self.role,
            model_role=self.model_role,
            scope_paths=self.files_out,
            dependencies=self.dependencies,
            dependency_kind=DependencyKind.DEPENDENT if self.dependencies else DependencyKind.INDEPENDENT,
        )


class SmartTaskGraph:
    """Constructs and optimizes task dependency graphs with cycle detection and autoscaling."""

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}

    def add_node(self, node: GraphNode) -> None:
        self._nodes[node.task_id] = node

    def detect_cycles(self) -> list[str] | None:
        """Detects if there is any cycle in the graph using Tarjan's/DFS algorithm."""
        visited: dict[str, int] = {}  # 0: unvisited, 1: visiting, 2: visited

        def _dfs(node_id: str, path: list[str]) -> list[str] | None:
            visited[node_id] = 1
            node = self._nodes.get(node_id)
            if node:
                for dep in node.dependencies:
                    if dep not in self._nodes:
                        continue
                    if visited.get(dep) == 1:
                        return path + [dep]
                    if visited.get(dep, 0) == 0:
                        res = _dfs(dep, path + [dep])
                        if res:
                            return res
            visited[node_id] = 2
            return None

        for nid in self._nodes:
            if visited.get(nid, 0) == 0:
                cycle = _dfs(nid, [nid])
                if cycle:
                    return cycle
        return None

    def calculate_optimal_concurrency(
        self,
        max_system_limit: int = 8,
        provider_limit: int = 4,
    ) -> int:
        """Calculates optimal worker concurrency: min(independent_tasks, safe_worker_capacity, provider_limit)."""
        # Count ready independent tasks
        independent_count = sum(1 for n in self._nodes.values() if not n.dependencies)
        cpu_count = os.cpu_count() or 4
        safe_worker_capacity = max(2, min(cpu_count, max_system_limit))
        optimal = max(1, min(independent_count or 1, safe_worker_capacity, provider_limit))
        return optimal

    def to_tasks(self) -> list[Task]:
        """Converts graph nodes to orchestrator tasks sorted by priority."""
        nodes = sorted(self._nodes.values(), key=lambda n: int(n.priority), reverse=True)
        return [n.to_task() for n in nodes]
