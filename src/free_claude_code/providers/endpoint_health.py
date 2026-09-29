"""Per-endpoint (provider + key) health with M0005 ADR-019 circuit semantics.

- rate limited -> DEGRADED + cooldown (retry-after, else 20 s); never opens the circuit
- quota exhausted -> DEGRADED + 1 h cooldown
- transient (timeout / unavailable / network / malformed) -> consecutive failure count;
  3 in a row -> OPEN for 30 s -> HALF_OPEN admits exactly one probe; success closes
- auth failure -> OPEN until manual reset
- no-impact failures (bad request, context length, cancelled) are ignored

Cooldowns and circuits persist through :class:`~free_claude_code.core.storage.Store`
so they survive restarts; timestamps are wall-clock epoch seconds from an injectable clock.
"""

import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from enum import StrEnum

from free_claude_code.core.json_types import JsonObject
from free_claude_code.core.storage import Store

Clock = Callable[[], float]

# ponytail: no "slot freed" signal; callers poll at this interval when only
# concurrency or an in-flight half-open probe blocks an endpoint.
CAPACITY_POLL_SECONDS = 0.2
_RECENT_ERRORS = 5


class CircuitState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class HealthSignal(StrEnum):
    """Health meaning of one failed attempt (derived from FailureKind by providers)."""

    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_FAILED = "auth_failed"
    TRANSIENT = "transient"
    NO_IMPACT = "no_impact"


@dataclass(frozen=True, slots=True)
class HealthPolicy:
    failure_threshold: int = 3
    open_cooldown_s: float = 30.0
    rate_limit_cooldown_s: float = 20.0
    quota_cooldown_s: float = 3600.0
    latency_window: int = 50


@dataclass(slots=True)
class EndpointHealth:
    provider_id: str
    label: str
    fingerprint: str
    circuit: CircuitState = CircuitState.HEALTHY
    cooldown_until: float | None = None
    cooldown_reason: str | None = None
    consecutive_failures: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_error: str | None = None
    last_error_at: float | None = None
    last_success_at: float | None = None
    updated_at: float = 0.0
    in_flight: int = 0
    probe_in_flight: bool = False
    last_selected: int = 0
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=50))
    recent_errors: deque[tuple[float, str]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_ERRORS)
    )


type EndpointKey = tuple[str, str]

_MIGRATIONS = [
    "CREATE TABLE endpoint_health ("
    " provider_id TEXT NOT NULL, label TEXT NOT NULL, fingerprint TEXT NOT NULL,"
    " circuit TEXT NOT NULL, cooldown_until REAL, cooldown_reason TEXT,"
    " consecutive_failures INTEGER NOT NULL, success_count INTEGER NOT NULL,"
    " failure_count INTEGER NOT NULL, last_error TEXT, last_error_at REAL,"
    " last_success_at REAL, updated_at REAL NOT NULL,"
    " PRIMARY KEY (provider_id, label))",
]


def percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of an already sorted, non-empty list."""
    index = min(len(sorted_values) - 1, int(fraction * len(sorted_values)))
    return sorted_values[index]


class EndpointHealthTracker:
    """In-memory endpoint health mirrored to SQLite on every transition."""

    def __init__(
        self,
        store: Store | None,
        *,
        policy: HealthPolicy | None = None,
        clock: Clock = time.time,
    ) -> None:
        self._store = store
        self._policy = policy or HealthPolicy()
        self._clock = clock
        self._endpoints: dict[EndpointKey, EndpointHealth] = {}
        self._selections = 0
        if store is not None:
            store.migrate("endpoint_health", _MIGRATIONS)
            for row in store.query("SELECT * FROM endpoint_health"):
                health = self._new(row["provider_id"], row["label"], row["fingerprint"])
                health.circuit = CircuitState(row["circuit"])
                health.cooldown_until = row["cooldown_until"]
                health.cooldown_reason = row["cooldown_reason"]
                health.consecutive_failures = row["consecutive_failures"]
                health.success_count = row["success_count"]
                health.failure_count = row["failure_count"]
                health.last_error = row["last_error"]
                health.last_error_at = row["last_error_at"]
                health.last_success_at = row["last_success_at"]
                health.updated_at = row["updated_at"]
                self._endpoints[(health.provider_id, health.label)] = health

    @property
    def policy(self) -> HealthPolicy:
        return self._policy

    def now(self) -> float:
        return self._clock()

    def _new(self, provider_id: str, label: str, fingerprint: str) -> EndpointHealth:
        return EndpointHealth(
            provider_id=provider_id,
            label=label,
            fingerprint=fingerprint,
            updated_at=self._clock(),
            latencies_ms=deque(maxlen=self._policy.latency_window),
        )

    def register(self, provider_id: str, label: str, fingerprint: str) -> None:
        """Track an endpoint; a replaced key under the same label starts healthy."""
        key = (provider_id, label)
        current = self._endpoints.get(key)
        if current is not None and current.fingerprint == fingerprint:
            return
        self._endpoints[key] = self._new(provider_id, label, fingerprint)
        self._persist(self._endpoints[key])

    def get(self, provider_id: str, label: str) -> EndpointHealth | None:
        health = self._endpoints.get((provider_id, label))
        if health is not None:
            self._refresh(health, self._clock())
        return health

    def endpoints(self) -> list[EndpointHealth]:
        now = self._clock()
        for health in self._endpoints.values():
            self._refresh(health, now)
        return list(self._endpoints.values())

    def _refresh(self, health: EndpointHealth, now: float) -> None:
        """Lazily flip a timed OPEN circuit to HALF_OPEN once its cooldown elapsed."""
        if (
            health.circuit is CircuitState.OPEN
            and health.cooldown_until is not None
            and now >= health.cooldown_until
        ):
            health.circuit = CircuitState.HALF_OPEN
            health.cooldown_until = None
            health.cooldown_reason = None
            health.updated_at = now
            self._persist(health)

    def _cooling(self, health: EndpointHealth, now: float) -> bool:
        return health.cooldown_until is not None and now < health.cooldown_until

    def can_accept(self, health: EndpointHealth, max_concurrency: int) -> bool:
        now = self._clock()
        self._refresh(health, now)
        if health.circuit is CircuitState.OPEN or self._cooling(health, now):
            return False
        if health.circuit is CircuitState.HALF_OPEN and health.probe_in_flight:
            return False
        return health.in_flight < max_concurrency

    def try_acquire(self, health: EndpointHealth, max_concurrency: int) -> bool:
        if not self.can_accept(health, max_concurrency):
            return False
        health.in_flight += 1
        self._selections += 1
        health.last_selected = self._selections
        if health.circuit is CircuitState.HALF_OPEN:
            health.probe_in_flight = True
        return True

    def release(self, health: EndpointHealth) -> None:
        health.in_flight = max(0, health.in_flight - 1)
        health.probe_in_flight = False

    def earliest_availability(
        self, endpoints: Iterable[EndpointHealth], max_concurrency: int
    ) -> float | None:
        """Epoch seconds when one endpoint can accept; None if all need manual reset."""
        now = self._clock()
        earliest: float | None = None
        for health in endpoints:
            self._refresh(health, now)
            if health.circuit is CircuitState.OPEN and health.cooldown_until is None:
                continue
            if self._cooling(health, now) and health.cooldown_until is not None:
                at = health.cooldown_until
            elif (
                health.circuit is CircuitState.HALF_OPEN and health.probe_in_flight
            ) or health.in_flight >= max_concurrency:
                at = now + CAPACITY_POLL_SECONDS
            else:
                return now
            earliest = at if earliest is None else min(earliest, at)
        return earliest

    def record_success(self, health: EndpointHealth, latency_ms: float) -> None:
        now = self._clock()
        health.consecutive_failures = 0
        health.success_count += 1
        health.last_success_at = now
        health.latencies_ms.append(latency_ms)
        health.probe_in_flight = False
        health.circuit = CircuitState.HEALTHY
        health.cooldown_until = None
        health.cooldown_reason = None
        health.updated_at = now
        self._persist(health)

    def record_failure(
        self,
        health: EndpointHealth,
        signal: HealthSignal,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        if signal is HealthSignal.NO_IMPACT:
            return
        now = self._clock()
        policy = self._policy
        was_half_open = health.circuit is CircuitState.HALF_OPEN
        health.probe_in_flight = False
        health.failure_count += 1
        health.last_error = signal.value
        health.last_error_at = now
        health.recent_errors.append((now, signal.value))
        health.updated_at = now
        if signal in (HealthSignal.RATE_LIMITED, HealthSignal.QUOTA_EXHAUSTED):
            default = (
                policy.rate_limit_cooldown_s
                if signal is HealthSignal.RATE_LIMITED
                else policy.quota_cooldown_s
            )
            cooldown = retry_after_s if retry_after_s is not None else default
            health.cooldown_until = now + max(0.0, cooldown)
            health.cooldown_reason = signal.value
            if health.circuit is CircuitState.HEALTHY or was_half_open:
                health.circuit = CircuitState.DEGRADED
        elif signal is HealthSignal.AUTH_FAILED:
            health.circuit = CircuitState.OPEN
            health.cooldown_until = None
            health.cooldown_reason = signal.value
        else:
            health.consecutive_failures = (
                policy.failure_threshold
                if was_half_open
                else health.consecutive_failures + 1
            )
            if health.consecutive_failures >= policy.failure_threshold:
                health.circuit = CircuitState.OPEN
                health.cooldown_until = now + policy.open_cooldown_s
                health.cooldown_reason = "circuit_open"
            elif health.circuit is CircuitState.HEALTHY:
                health.circuit = CircuitState.DEGRADED
        self._persist(health)

    def reset(self, provider_id: str, label: str) -> bool:
        current = self._endpoints.get((provider_id, label))
        if current is None:
            return False
        fresh = self._new(provider_id, label, current.fingerprint)
        fresh.in_flight = current.in_flight
        fresh.last_selected = current.last_selected
        # In place: callers holding this endpoint must release the same object.
        for item in fields(fresh):
            setattr(current, item.name, getattr(fresh, item.name))
        self._persist(current)
        return True

    def snapshot(self, health: EndpointHealth) -> JsonObject:
        now = self._clock()
        self._refresh(health, now)
        latencies = sorted(health.latencies_ms)
        cooldown_remaining = (
            round(max(0.0, health.cooldown_until - now), 3)
            if health.cooldown_until is not None
            else None
        )
        return {
            "provider_id": health.provider_id,
            "label": health.label,
            "circuit": health.circuit.value,
            "available": self.can_accept(health, max_concurrency=1 << 30),
            "cooldown_reason": health.cooldown_reason,
            "cooldown_until": health.cooldown_until,
            "cooldown_remaining_s": cooldown_remaining,
            "manual_reset_required": health.circuit is CircuitState.OPEN
            and health.cooldown_until is None,
            "consecutive_failures": health.consecutive_failures,
            "success_count": health.success_count,
            "failure_count": health.failure_count,
            "in_flight": health.in_flight,
            "latency_p50_ms": (
                round(percentile(latencies, 0.5), 1) if latencies else None
            ),
            "latency_p95_ms": (
                round(percentile(latencies, 0.95), 1) if latencies else None
            ),
            "last_error": health.last_error,
            "last_error_at": health.last_error_at,
            "last_success_at": health.last_success_at,
            "recent_errors": [
                {"at": at, "signal": code} for at, code in health.recent_errors
            ],
        }

    def _persist(self, health: EndpointHealth) -> None:
        if self._store is None:
            return
        # ponytail: synchronous SQLite write per transition (~ms, WAL); move to a
        # background writer if transitions ever show up in latency profiles.
        self._store.execute(
            "INSERT INTO endpoint_health (provider_id, label, fingerprint, circuit,"
            " cooldown_until, cooldown_reason, consecutive_failures, success_count,"
            " failure_count, last_error, last_error_at, last_success_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(provider_id, label) DO UPDATE SET"
            " fingerprint = excluded.fingerprint, circuit = excluded.circuit,"
            " cooldown_until = excluded.cooldown_until,"
            " cooldown_reason = excluded.cooldown_reason,"
            " consecutive_failures = excluded.consecutive_failures,"
            " success_count = excluded.success_count,"
            " failure_count = excluded.failure_count,"
            " last_error = excluded.last_error, last_error_at = excluded.last_error_at,"
            " last_success_at = excluded.last_success_at,"
            " updated_at = excluded.updated_at",
            (
                health.provider_id,
                health.label,
                health.fingerprint,
                health.circuit.value,
                health.cooldown_until,
                health.cooldown_reason,
                health.consecutive_failures,
                health.success_count,
                health.failure_count,
                health.last_error,
                health.last_error_at,
                health.last_success_at,
                health.updated_at,
            ),
        )
