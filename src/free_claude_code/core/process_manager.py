from __future__ import annotations

"""Centralized Windows-native subprocess manager with bounded timeouts and tree termination."""

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class ProcessKind(StrEnum):
    SHORT_TASK = "SHORT_TASK"
    TEST = "TEST"
    BENCHMARK = "BENCHMARK"
    BACKGROUND_SERVICE = "BACKGROUND_SERVICE"
    WATCHER = "WATCHER"
    AGENT_WORKER = "AGENT_WORKER"


@dataclass
class ManagedProcess:
    name: str
    kind: ProcessKind
    process: subprocess.Popen[str]
    command: list[str]
    start_time: float = field(default_factory=time.time)
    timeout_seconds: float = 60.0
    stdout_buffer: list[str] = field(default_factory=list)
    stderr_buffer: list[str] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def is_running(self) -> bool:
        return self.process.poll() is None

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time


class SubprocessManager:
    """Manages creation, monitoring, timeout enforcement, and clean termination of subprocesses."""

    def __init__(self) -> None:
        self._processes: dict[int, ManagedProcess] = {}
        self._lock = threading.Lock()

    def spawn(
        self,
        command: list[str],
        name: str,
        kind: ProcessKind = ProcessKind.SHORT_TASK,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 60.0,
    ) -> ManagedProcess:
        """Spawns a managed child process with output capture."""
        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        popen_kwargs: dict = {
            "cwd": cwd or os.getcwd(),
            "env": full_env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }

        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(command, **popen_kwargs)
        managed = ManagedProcess(
            name=name,
            kind=kind,
            process=proc,
            command=command,
            timeout_seconds=timeout_seconds,
        )

        with self._lock:
            self._processes[proc.pid] = managed

        # Start output reading threads to prevent pipe deadlocks
        def _read_stdout():
            try:
                for line in proc.stdout:
                    managed.stdout_buffer.append(line)
            except Exception:
                pass

        def _read_stderr():
            try:
                for line in proc.stderr:
                    managed.stderr_buffer.append(line)
            except Exception:
                pass

        threading.Thread(target=_read_stdout, daemon=True).start()
        threading.Thread(target=_read_stderr, daemon=True).start()

        return managed

    def wait_with_timeout(self, managed: ManagedProcess, timeout: float | None = None) -> int:
        """Waits for a finite process to exit within the timeout period."""
        eff_timeout = timeout if timeout is not None else managed.timeout_seconds
        try:
            return managed.process.wait(timeout=eff_timeout)
        except subprocess.TimeoutExpired:
            self.terminate_tree(managed.pid)
            raise TimeoutError(f"Process '{managed.name}' (PID {managed.pid}) exceeded timeout of {eff_timeout}s")
        finally:
            with self._lock:
                self._processes.pop(managed.pid, None)

    def terminate_tree(self, pid: int) -> None:
        """Terminates a process and all its child subprocesses cleanly on Windows or Unix."""
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except Exception:
                pass
        else:
            try:
                import signal
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                try:
                    os.kill(pid, 9)
                except Exception:
                    pass

        with self._lock:
            self._processes.pop(pid, None)

    def terminate_all(self, kind: ProcessKind | None = None) -> None:
        """Terminates all tracked managed processes."""
        with self._lock:
            pids = [
                m.pid
                for m in self._processes.values()
                if kind is None or m.kind == kind
            ]
        for pid in pids:
            self.terminate_tree(pid)


_GLOBAL_PROCESS_MANAGER = SubprocessManager()


def get_process_manager() -> SubprocessManager:
    return _GLOBAL_PROCESS_MANAGER
