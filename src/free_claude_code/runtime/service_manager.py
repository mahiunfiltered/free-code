from __future__ import annotations

"""Service lifecycle manager for long-running background servers (e.g. fcc-server).

Ensures background services are started asynchronously, checked for health readiness,
and never block finite orchestration workflows.
"""

import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field

from free_claude_code.core.process_manager import (
    ProcessKind,
    get_process_manager,
)


@dataclass
class ServiceStatus:
    name: str
    pid: int
    health_url: str
    is_ready: bool
    startup_seconds: float
    error: str | None = None


class BackgroundServiceManager:
    """Manages background service processes (fcc-server, proxy gateways, mock servers)."""

    def __init__(self) -> None:
        self.process_manager = get_process_manager()
        self._services: dict[str, ServiceStatus] = {}

    def is_service_ready(self, health_url: str, timeout: float = 1.0) -> bool:
        """Probes a health endpoint for 200 OK status."""
        try:
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def start_service(
        self,
        name: str,
        command: list[str],
        health_url: str,
        startup_timeout: float = 15.0,
        cwd: str | None = None,
    ) -> ServiceStatus:
        """Starts a background service and waits for readiness without blocking orchestration."""
        start_time = time.time()

        # Check if already running and healthy
        if self.is_service_ready(health_url, timeout=0.5):
            status = ServiceStatus(
                name=name,
                pid=0,
                health_url=health_url,
                is_ready=True,
                startup_seconds=0.0,
            )
            self._services[name] = status
            return status

        managed = self.process_manager.spawn(
            command=command,
            name=name,
            kind=ProcessKind.BACKGROUND_SERVICE,
            cwd=cwd,
            timeout_seconds=3600.0,  # Background service lives until explicitly stopped
        )

        # Poll readiness until startup_timeout
        ready = False
        while time.time() - start_time < startup_timeout:
            if not managed.is_running:
                err_msg = "".join(managed.stderr_buffer) or "Process exited prematurely"
                status = ServiceStatus(
                    name=name,
                    pid=managed.pid,
                    health_url=health_url,
                    is_ready=False,
                    startup_seconds=time.time() - start_time,
                    error=err_msg,
                )
                self._services[name] = status
                return status

            if self.is_service_ready(health_url, timeout=0.5):
                ready = True
                break
            time.sleep(0.5)

        status = ServiceStatus(
            name=name,
            pid=managed.pid,
            health_url=health_url,
            is_ready=ready,
            startup_seconds=time.time() - start_time,
            error=None if ready else f"Service did not become ready within {startup_timeout}s",
        )
        self._services[name] = status
        return status

    def stop_service(self, name: str) -> None:
        """Stops a background service."""
        if status := self._services.pop(name, None):
            if status.pid > 0:
                self.process_manager.terminate_tree(status.pid)

    def stop_all(self) -> None:
        """Stops all running background services."""
        for name in list(self._services.keys()):
            self.stop_service(name)


_GLOBAL_SERVICE_MANAGER = BackgroundServiceManager()


def get_service_manager() -> BackgroundServiceManager:
    return _GLOBAL_SERVICE_MANAGER
