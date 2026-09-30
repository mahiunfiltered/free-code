"""File conflict protection and path scope locking for parallel agents."""

import os
import threading
from dataclasses import dataclass, field


@dataclass
class ConflictManager:
    """Manages file ownership and prevents concurrent conflicting writes with reentrant locking."""

    _locked_paths: dict[str, str] = field(default_factory=dict)  # path -> task_id
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def _normalize_path(self, path: str) -> str:
        return os.path.normpath(os.path.abspath(path)).lower()

    def _can_acquire_unlocked(self, task_id: str, paths: list[str]) -> bool:
        for p in paths:
            norm = self._normalize_path(p)
            for locked_p, owner_id in self._locked_paths.items():
                if owner_id == task_id:
                    continue
                if (
                    norm == locked_p
                    or norm.startswith(locked_p + os.sep)
                    or locked_p.startswith(norm + os.sep)
                ):
                    return False
        return True

    def can_acquire_scope(self, task_id: str, paths: list[str]) -> bool:
        """Checks if a task can acquire all requested path scopes without conflict."""
        with self._lock:
            return self._can_acquire_unlocked(task_id, paths)

    def acquire_scope(self, task_id: str, paths: list[str]) -> bool:
        """Acquires lock for the given path scopes."""
        with self._lock:
            if not self._can_acquire_unlocked(task_id, paths):
                return False
            for p in paths:
                norm = self._normalize_path(p)
                self._locked_paths[norm] = task_id
            return True

    def release_scope(self, task_id: str) -> None:
        """Releases all locks held by a task."""
        with self._lock:
            to_remove = [
                p for p, owner in self._locked_paths.items() if owner == task_id
            ]
            for p in to_remove:
                self._locked_paths.pop(p, None)

    def get_conflicting_owner(self, path: str) -> str | None:
        """Returns the task_id owning a conflicting path lock, if any."""
        with self._lock:
            norm = self._normalize_path(path)
            for locked_p, owner_id in self._locked_paths.items():
                if (
                    norm == locked_p
                    or norm.startswith(locked_p + os.sep)
                    or locked_p.startswith(norm + os.sep)
                ):
                    return owner_id
            return None
