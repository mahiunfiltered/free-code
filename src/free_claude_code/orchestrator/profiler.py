from __future__ import annotations

"""Real-time execution profiler and performance metric logger."""

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from free_claude_code.orchestrator.models import Task, TaskState


@dataclass
class MetricRecord:
    operation: str
    start_time: float
    end_time: float
    duration_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProfilerSnapshot:
    project_name: str
    elapsed_seconds: float
    active_agents_count: int
    completed_agents_count: int
    blocked_agents_count: int
    total_model_requests: int
    avg_model_latency_seconds: float
    total_tool_calls: int
    avg_tool_latency_seconds: float
    current_bottleneck: str
    estimated_eta_seconds: float


class RuntimeProfiler:
    """Collects real-time runtime metrics and generates performance baselines."""

    def __init__(self, project_name: str = "Claude Code Runtime") -> None:
        self.project_name = project_name
        self.start_time = time.time()
        self._metrics: list[MetricRecord] = []
        self._model_latencies: list[float] = []
        self._tool_latencies: list[float] = []
        self.total_tokens_estimated: int = 0
        self.recovery_attempts: int = 0

    def record_model_call(self, duration: float, model: str = "", tokens: int = 0) -> None:
        self._model_latencies.append(duration)
        self.total_tokens_estimated += tokens
        self._metrics.append(
            MetricRecord(
                operation="model_request",
                start_time=time.time() - duration,
                end_time=time.time(),
                duration_seconds=duration,
                metadata={"model": model, "tokens": tokens},
            )
        )

    def record_tool_call(self, tool_name: str, duration: float, success: bool = True) -> None:
        self._tool_latencies.append(duration)
        self._metrics.append(
            MetricRecord(
                operation=f"tool_{tool_name}",
                start_time=time.time() - duration,
                end_time=time.time(),
                duration_seconds=duration,
                metadata={"success": success},
            )
        )

    def get_snapshot(self, tasks: Sequence[Task] = ()) -> ProfilerSnapshot:
        elapsed = time.time() - self.start_time
        active = sum(1 for t in tasks if t.status == TaskState.RUNNING)
        completed = sum(1 for t in tasks if t.status == TaskState.COMPLETED)
        blocked = sum(1 for t in tasks if t.status in (TaskState.BLOCKED, TaskState.FAILED))

        avg_model = sum(self._model_latencies) / max(1, len(self._model_latencies))
        avg_tool = sum(self._tool_latencies) / max(1, len(self._tool_latencies))

        # Identify bottleneck
        bottleneck = "None (Steady Execution)"
        if blocked > 0:
            bottleneck = "Blocked dependencies / Recovery"
        elif active > 0:
            running_tasks = [t.name for t in tasks if t.status == TaskState.RUNNING]
            bottleneck = f"Active: {', '.join(running_tasks[:2])}"

        # Estimate remaining ETA
        remaining = len(tasks) - (completed + blocked)
        eta = (remaining * (avg_model + avg_tool)) / max(1, active) if active > 0 else 0.0

        return ProfilerSnapshot(
            project_name=self.project_name,
            elapsed_seconds=elapsed,
            active_agents_count=active,
            completed_agents_count=completed,
            blocked_agents_count=blocked,
            total_model_requests=len(self._model_latencies),
            avg_model_latency_seconds=avg_model,
            total_tool_calls=len(self._tool_latencies),
            avg_tool_latency_seconds=avg_tool,
            current_bottleneck=bottleneck,
            estimated_eta_seconds=eta,
        )

    def export_baseline(self, filepath: str = "performance_baseline.json") -> dict[str, Any]:
        """Exports complete performance baseline to JSON."""
        data = {
            "project_name": self.project_name,
            "timestamp": time.time(),
            "total_duration_seconds": time.time() - self.start_time,
            "total_model_requests": len(self._model_latencies),
            "avg_model_latency_seconds": sum(self._model_latencies) / max(1, len(self._model_latencies)),
            "total_tool_calls": len(self._tool_latencies),
            "avg_tool_latency_seconds": sum(self._tool_latencies) / max(1, len(self._tool_latencies)),
            "total_tokens_estimated": self.total_tokens_estimated,
            "recovery_attempts": self.recovery_attempts,
            "metrics_count": len(self._metrics),
        }
        with open(filepath, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)
        return data


_GLOBAL_PROFILER = RuntimeProfiler()


def get_runtime_profiler() -> RuntimeProfiler:
    return _GLOBAL_PROFILER
