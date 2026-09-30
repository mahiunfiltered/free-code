"""Terminal progress dashboard for parallel agent execution."""

import sys
import time
from collections.abc import Sequence

from free_claude_code.orchestrator.models import Task, TaskState


class ProgressDashboard:
    """Renders a concise, human-readable terminal dashboard."""

    def __init__(self, title: str = "Claude Code Autonomous Runtime") -> None:
        self.title = title
        self.start_time = time.time()

    def _state_icon(self, state: TaskState) -> str:
        icons = {
            TaskState.COMPLETED: "[OK] ✓",
            TaskState.RUNNING: "[RUN] ●",
            TaskState.QUEUED: "[WAIT] ○",
            TaskState.WAITING: "[LOCK] ◐",
            TaskState.FAILED: "[FAIL] ✗",
            TaskState.BLOCKED: "[BLCK] ⊘",
            TaskState.RETRYING: "[RTRY] ↻",
            TaskState.REVIEWING: "[REVW] 🔍",
            TaskState.PLANNING: "[PLAN] 📋",
            TaskState.CREATED: "[INIT] ◌",
            TaskState.CANCELLED: "[CNCL] ⊗",
        }
        return icons.get(state, "[INFO] ·")

    def render(
        self,
        master_state: str,
        tasks: Sequence[Task],
        model_name: str = "nvidia/nemotron-3-super-120b-a12b",
        stream=sys.stdout,
    ) -> None:
        elapsed = time.time() - self.start_time
        lines: list[str] = []
        lines.append(f"\n{'=' * 60}")
        lines.append(f" {self.title}")
        lines.append(f"{'=' * 60}")
        lines.append(f" MASTER:  {master_state:<16}  Elapsed: {elapsed:.1f}s")
        lines.append(f" MODEL:   {model_name}")
        lines.append(f"{'-' * 60}")
        lines.append(f" {'STATE':<10} {'ROLE':<12} {'TASK':<24} {'TIME':<8}")
        lines.append(f"{'-' * 60}")

        for t in tasks:
            icon = self._state_icon(t.status)
            role_str = t.role.value
            name_str = (t.name[:21] + "...") if len(t.name) > 24 else t.name
            dur_str = f"{t.duration_seconds:.1f}s"
            lines.append(f" {icon:<10} {role_str:<12} {name_str:<24} {dur_str:<8}")

        lines.append(f"{'=' * 60}\n")
        print("\n".join(lines), file=stream, flush=True)
