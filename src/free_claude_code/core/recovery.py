from __future__ import annotations

"""Bounded error recovery and smart retry engine.

Provides:
- Classification of transient vs permanent errors
- Exponential backoff with jitter
- Bounded auto-repair loop (maximum 3 attempts)
- Diagnosis and actionable reports on BLOCKED states
"""

import asyncio
import random
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

T = TypeVar("T")


class ErrorSeverity(StrEnum):
    TRANSIENT = "transient"      # 429, 500, 502, 503, 504, network timeout -> Retry
    PERMANENT = "permanent"      # 400, 401, 403, 404, invalid auth, schema -> Fail fast
    RECOVERABLE = "recoverable"  # Command syntax error, missing path -> Auto-fix & retry
    BLOCKED = "blocked"          # Max attempts exceeded -> Human action needed


@dataclass
class FailureReport:
    task_id: str
    error_message: str
    severity: ErrorSeverity
    attempt_count: int
    attempted_fixes: list[str] = field(default_factory=list)
    recommended_action: str = ""
    timestamp: float = field(default_factory=time.time)


class CircuitBreakerState(StrEnum):
    CLOSED = "CLOSED"        # Normal operation
    OPEN = "OPEN"            # Provider tripped, blocking requests
    HALF_OPEN = "HALF_OPEN"  # Testing single probe request


class CircuitBreaker:
    """Circuit breaker for repeatedly failing model providers."""

    def __init__(self, failure_threshold: int = 5, recovery_timeout_seconds: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.failure_count: int = 0
        self.last_failure_time: float = 0.0
        self.state: CircuitBreakerState = CircuitBreakerState.CLOSED

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = CircuitBreakerState.CLOSED

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitBreakerState.OPEN

    def allow_request(self) -> bool:
        if self.state == CircuitBreakerState.CLOSED:
            return True
        if self.state == CircuitBreakerState.OPEN:
            if time.time() - self.last_failure_time >= self.recovery_timeout_seconds:
                self.state = CircuitBreakerState.HALF_OPEN
                return True
            return False
        if self.state == CircuitBreakerState.HALF_OPEN:
            return True
        return True


class ApiProgressReporter:
    """Logs clean observable progress milestones for API and streaming interactions."""

    @staticmethod
    def on_connecting(endpoint: str) -> None:
        print(f"[API] Connecting to {endpoint}...")

    @staticmethod
    def on_connected(model: str) -> None:
        print(f"[API] Connected | Model: {model}")

    @staticmethod
    def on_ttft(ttft_seconds: float) -> None:
        print(f"[API] TTFT: {ttft_seconds:.3f}s")

    @staticmethod
    def on_streaming(chunk_count: int) -> None:
        if chunk_count % 20 == 0:
            print(f"[API] Streaming... ({chunk_count} chunks received)")

    @staticmethod
    def on_completed(duration_seconds: float, token_estimate: int = 0) -> None:
        print(f"[API] Completed in {duration_seconds:.2f}s (~{token_estimate} tokens)")


def classify_error(exc: Exception | str) -> ErrorSeverity:
    """Classifies an error as transient, permanent, or recoverable."""
    msg = str(exc).lower()

    # Shell and path recoverable errors
    if any(k in msg for k in ("command not found", "not recognized as an internal or external command", "no such file or directory", "exit code 127", "exit code 1")):
        return ErrorSeverity.RECOVERABLE

    # Permanent authentication & request errors
    if any(k in msg for k in ("401", "403", "invalid api key", "unauthorized", "forbidden", "404", "model_not_found", "invalid_request_error")):
        return ErrorSeverity.PERMANENT

    # Transient network & rate limit errors
    if any(k in msg for k in ("429", "rate limit", "500", "502", "503", "504", "timeout", "connection reset", "econnreset")):
        return ErrorSeverity.TRANSIENT

    return ErrorSeverity.TRANSIENT


class BoundedRecoveryEngine:
    """Executes actions with bounded retries, auto-diagnosis, and exponential backoff."""

    def __init__(self, max_attempts: int = 3, base_backoff_seconds: float = 1.0) -> None:
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds
        self.reports: list[FailureReport] = []
        self.circuit_breakers: dict[str, CircuitBreaker] = {}

    def get_circuit_breaker(self, provider_id: str = "default") -> CircuitBreaker:
        if provider_id not in self.circuit_breakers:
            self.circuit_breakers[provider_id] = CircuitBreaker()
        return self.circuit_breakers[provider_id]

    async def execute_with_recovery(
        self,
        task_id: str,
        coro_fn: Callable[[], Coroutine[Any, Any, T]],
        diagnose_and_repair_fn: Callable[[Exception, int], Coroutine[Any, Any, bool]] | None = None,
        provider_id: str = "default",
    ) -> T:
        """Executes a coroutine with up to max_attempts retries, circuit breaking, and automated diagnosis."""
        breaker = self.get_circuit_breaker(provider_id)
        if not breaker.allow_request():
            raise RuntimeError(f"Circuit breaker OPEN for provider '{provider_id}'. Failing fast.")

        attempt = 0
        attempted_fixes: list[str] = []

        while True:
            attempt += 1
            try:
                result = await coro_fn()
                breaker.record_success()
                return result
            except Exception as exc:
                severity = classify_error(exc)
                breaker.record_failure()

                if severity == ErrorSeverity.PERMANENT:
                    report = FailureReport(
                        task_id=task_id,
                        error_message=str(exc),
                        severity=ErrorSeverity.PERMANENT,
                        attempt_count=attempt,
                        attempted_fixes=attempted_fixes,
                        recommended_action="Check authentication credentials, endpoint URL, or requested resource permissions.",
                    )
                    self.reports.append(report)
                    raise

                if attempt >= self.max_attempts:
                    report = FailureReport(
                        task_id=task_id,
                        error_message=f"Exceeded {self.max_attempts} attempts: {exc}",
                        severity=ErrorSeverity.BLOCKED,
                        attempt_count=attempt,
                        attempted_fixes=attempted_fixes,
                        recommended_action=f"Automated recovery failed after {attempt} attempts. Check task parameters and logs.",
                    )
                    self.reports.append(report)
                    raise RuntimeError(f"Task {task_id} BLOCKED: {report.error_message}") from exc

                # Attempt diagnosis and repair if available
                if diagnose_and_repair_fn and severity == ErrorSeverity.RECOVERABLE:
                    fix_applied = await diagnose_and_repair_fn(exc, attempt)
                    if fix_applied:
                        attempted_fixes.append(f"Attempt {attempt}: Applied diagnostic repair for '{exc}'")

                # Exponential backoff with jitter
                delay = (self.base_backoff_seconds * (2 ** (attempt - 1))) + random.uniform(0.1, 0.5)
                await asyncio.sleep(delay)
