from __future__ import annotations

"""Anti-stagnation watchdog and activity monitor.

Monitors active tasks and system operations. If no meaningful progress occurs
within the configured threshold (default 30 seconds), diagnoses the bottleneck
(API hang, shell deadlock, worker stall) and triggers graceful remediation.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ActivityKind(StrEnum):
    API_REQUEST = "api_request"
    SHELL_COMMAND = "shell_command"
    HOOK_EXECUTION = "hook_execution"
    WORKER_TASK = "worker_task"
    IDLE = "idle"


@dataclass
class ActivityRecord:
    activity_id: str
    kind: ActivityKind
    description: str
    start_time: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    is_active: bool = True

    def heartbeat(self) -> None:
        self.last_heartbeat = time.time()

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_heartbeat


class AntiStagnationWatchdog:
    """Monitors running operations and detects stagnation without blocking."""

    def __init__(
        self,
        inactivity_timeout_seconds: float = 30.0,
        on_stagnation: Callable[[ActivityRecord], None] | None = None,
    ) -> None:
        self.inactivity_timeout_seconds = inactivity_timeout_seconds
        self.on_stagnation = on_stagnation
        self._activities: dict[str, ActivityRecord] = {}
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="fcc-watchdog"
            )
            self._monitor_thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False

    def register_activity(
        self,
        activity_id: str,
        kind: ActivityKind,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> ActivityRecord:
        record = ActivityRecord(
            activity_id=activity_id,
            kind=kind,
            description=description,
            metadata=metadata or {},
        )
        with self._lock:
            self._activities[activity_id] = record
        return record

    def heartbeat(self, activity_id: str) -> None:
        with self._lock:
            if record := self._activities.get(activity_id):
                record.heartbeat()

    def finish_activity(self, activity_id: str) -> None:
        with self._lock:
            if record := self._activities.get(activity_id):
                record.is_active = False
                self._activities.pop(activity_id, None)

    def get_active_activities(self) -> list[ActivityRecord]:
        with self._lock:
            return [a for a in self._activities.values() if a.is_active]

    def _monitor_loop(self) -> None:
        while self._running:
            time.sleep(2.0)
            now = time.time()
            stalled: list[ActivityRecord] = []
            with self._lock:
                for act in self._activities.values():
                    if act.is_active and (now - act.last_heartbeat) > self.inactivity_timeout_seconds:
                        stalled.append(act)

            for act in stalled:
                if self.on_stagnation:
                    try:
                        self.on_stagnation(act)
                    except Exception:
                        pass
