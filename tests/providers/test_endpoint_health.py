"""Endpoint health state machine (M0005 ADR-019) with a fake clock."""

import pytest

from free_claude_code.core.storage import Store
from free_claude_code.providers.endpoint_health import (
    CAPACITY_POLL_SECONDS,
    CircuitState,
    EndpointHealth,
    EndpointHealthTracker,
    HealthPolicy,
    HealthSignal,
    percentile,
)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _tracker(clock: FakeClock, store: Store | None = None) -> EndpointHealthTracker:
    return EndpointHealthTracker(store, clock=clock)


def _endpoint(tracker: EndpointHealthTracker, label: str = "a") -> EndpointHealth:
    tracker.register("nim", label, f"fp-{label}")
    health = tracker.get("nim", label)
    assert health is not None
    return health


def test_new_endpoint_is_healthy_and_accepts(clock: FakeClock) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    assert health.circuit is CircuitState.HEALTHY
    assert tracker.can_accept(health, 1)


def test_rate_limit_degrades_with_default_cooldown_and_never_opens(
    clock: FakeClock,
) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    for _ in range(5):
        tracker.record_failure(health, HealthSignal.RATE_LIMITED)
    assert health.circuit is CircuitState.DEGRADED
    assert health.cooldown_until == pytest.approx(clock.now + 20.0)
    assert health.consecutive_failures == 0
    assert not tracker.can_accept(health, 1)
    clock.advance(19.9)
    assert not tracker.can_accept(health, 1)
    clock.advance(0.2)
    assert tracker.can_accept(health, 1)
    assert health.circuit is CircuitState.DEGRADED


def test_rate_limit_honors_provider_retry_after(clock: FakeClock) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    tracker.record_failure(health, HealthSignal.RATE_LIMITED, retry_after_s=3.5)
    assert health.cooldown_until == pytest.approx(clock.now + 3.5)
    assert health.cooldown_reason == "rate_limited"


def test_quota_exhausted_cools_down_for_one_hour(clock: FakeClock) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    tracker.record_failure(health, HealthSignal.QUOTA_EXHAUSTED)
    assert health.circuit is CircuitState.DEGRADED
    assert health.cooldown_until == pytest.approx(clock.now + 3600.0)
    clock.advance(3599)
    assert not tracker.can_accept(health, 1)
    clock.advance(2)
    assert tracker.can_accept(health, 1)


def test_three_transient_failures_open_then_half_open_single_probe(
    clock: FakeClock,
) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    tracker.record_failure(health, HealthSignal.TRANSIENT)
    assert health.circuit is CircuitState.DEGRADED
    tracker.record_failure(health, HealthSignal.TRANSIENT)
    assert health.circuit is CircuitState.DEGRADED
    assert tracker.can_accept(health, 1)
    tracker.record_failure(health, HealthSignal.TRANSIENT)
    assert health.circuit is CircuitState.OPEN
    assert health.cooldown_until == pytest.approx(clock.now + 30.0)
    assert not tracker.can_accept(health, 10)

    clock.advance(30)
    assert tracker.try_acquire(health, 10)
    assert health.circuit is CircuitState.HALF_OPEN
    # Exactly one probe while the first is in flight, regardless of concurrency.
    assert not tracker.can_accept(health, 10)
    assert not tracker.try_acquire(health, 10)
    tracker.record_success(health, 120.0)
    tracker.release(health)
    assert health.circuit is CircuitState.HEALTHY
    assert health.consecutive_failures == 0
    assert tracker.can_accept(health, 10)


def test_failed_half_open_probe_reopens_immediately(clock: FakeClock) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    for _ in range(3):
        tracker.record_failure(health, HealthSignal.TRANSIENT)
    clock.advance(30)
    assert tracker.try_acquire(health, 1)
    tracker.record_failure(health, HealthSignal.TRANSIENT)
    tracker.release(health)
    assert health.circuit is CircuitState.OPEN
    assert health.cooldown_until == pytest.approx(clock.now + 30.0)


def test_half_open_rate_limit_degrades_without_opening(clock: FakeClock) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    for _ in range(3):
        tracker.record_failure(health, HealthSignal.TRANSIENT)
    clock.advance(30)
    assert tracker.try_acquire(health, 1)
    tracker.record_failure(health, HealthSignal.RATE_LIMITED)
    assert health.circuit is CircuitState.DEGRADED


def test_abandoned_half_open_probe_releases_the_slot(clock: FakeClock) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    for _ in range(3):
        tracker.record_failure(health, HealthSignal.TRANSIENT)
    clock.advance(30)
    assert tracker.try_acquire(health, 1)
    tracker.release(health)
    assert tracker.try_acquire(health, 1)


def test_auth_failure_opens_until_manual_reset(clock: FakeClock) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    tracker.record_failure(health, HealthSignal.AUTH_FAILED)
    assert health.circuit is CircuitState.OPEN
    assert health.cooldown_until is None
    clock.advance(10 * 3600)
    assert not tracker.can_accept(health, 1)
    assert tracker.earliest_availability([health], 1) is None
    assert tracker.snapshot(health)["manual_reset_required"] is True

    assert tracker.reset("nim", "a")
    assert health.circuit is CircuitState.HEALTHY
    assert tracker.can_accept(health, 1)
    assert not tracker.reset("nim", "missing")


def test_no_impact_failures_leave_health_untouched(clock: FakeClock) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    tracker.record_failure(health, HealthSignal.NO_IMPACT)
    assert health.circuit is CircuitState.HEALTHY
    assert health.failure_count == 0
    assert health.last_error is None


def test_success_resets_consecutive_failures(clock: FakeClock) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    tracker.record_failure(health, HealthSignal.TRANSIENT)
    tracker.record_failure(health, HealthSignal.TRANSIENT)
    tracker.record_success(health, 50.0)
    tracker.record_failure(health, HealthSignal.TRANSIENT)
    assert health.circuit is CircuitState.DEGRADED
    assert health.consecutive_failures == 1


def test_concurrency_limit_blocks_and_earliest_availability_polls(
    clock: FakeClock,
) -> None:
    tracker = _tracker(clock)
    health = _endpoint(tracker)
    assert tracker.try_acquire(health, 1)
    assert not tracker.can_accept(health, 1)
    assert tracker.earliest_availability([health], 1) == pytest.approx(
        clock.now + CAPACITY_POLL_SECONDS
    )
    tracker.release(health)
    assert tracker.earliest_availability([health], 1) == clock.now


def test_earliest_availability_picks_soonest_cooldown(clock: FakeClock) -> None:
    tracker = _tracker(clock)
    first = _endpoint(tracker, "a")
    second = _endpoint(tracker, "b")
    tracker.record_failure(first, HealthSignal.RATE_LIMITED, retry_after_s=40)
    tracker.record_failure(second, HealthSignal.RATE_LIMITED, retry_after_s=5)
    assert tracker.earliest_availability([first, second], 1) == pytest.approx(
        clock.now + 5
    )


def test_latency_percentiles_over_rolling_window(clock: FakeClock) -> None:
    tracker = EndpointHealthTracker(
        None, clock=clock, policy=HealthPolicy(latency_window=10)
    )
    health = _endpoint(tracker)
    for latency in range(1, 101):
        tracker.record_success(health, float(latency))
    snapshot = tracker.snapshot(health)
    # Only the last 10 samples (91..100) remain.
    assert snapshot["latency_p50_ms"] == 96.0
    assert snapshot["latency_p95_ms"] == 100.0
    assert percentile([1.0], 0.95) == 1.0


def test_health_survives_store_reopen(clock: FakeClock, tmp_path) -> None:
    path = tmp_path / "fcc.db"
    store = Store(path)
    tracker = _tracker(clock, store)
    limited = _endpoint(tracker, "limited")
    broken = _endpoint(tracker, "broken")
    flaky = _endpoint(tracker, "flaky")
    tracker.record_failure(limited, HealthSignal.RATE_LIMITED, retry_after_s=50)
    tracker.record_failure(broken, HealthSignal.AUTH_FAILED)
    for _ in range(3):
        tracker.record_failure(flaky, HealthSignal.TRANSIENT)
    store.close()

    reopened = Store(path)
    restored = _tracker(clock, reopened)
    limited2 = restored.get("nim", "limited")
    broken2 = restored.get("nim", "broken")
    flaky2 = restored.get("nim", "flaky")
    assert limited2 is not None and broken2 is not None and flaky2 is not None
    assert limited2.circuit is CircuitState.DEGRADED
    assert limited2.cooldown_until == pytest.approx(clock.now + 50)
    assert broken2.circuit is CircuitState.OPEN and broken2.cooldown_until is None
    assert flaky2.circuit is CircuitState.OPEN
    # Registering the same key keeps persisted state; a replaced key starts fresh.
    restored.register("nim", "broken", "fp-broken")
    assert not restored.can_accept(broken2, 1)
    restored.register("nim", "limited", "fp-new-key")
    replaced = restored.get("nim", "limited")
    assert replaced is not None and replaced.circuit is CircuitState.HEALTHY
    clock.advance(30)
    assert restored.can_accept(flaky2, 1)
    reopened.close()
