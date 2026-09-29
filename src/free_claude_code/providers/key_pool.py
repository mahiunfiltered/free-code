"""Multi-key endpoint pool: healthiest-key selection, bounded failover, usage rows.

One :class:`PooledProvider` wraps one inner provider per API key. Retryable
failures (rate limit, quota, capacity, transport, auth) raised before the first
streamed chunk mark the key's health and fail over to the next eligible key, at
most ``max_attempts`` attempts. Once every key was tried, health decides: wait
for the earliest availability up to ``max_wait_s``, else fail with a rate-limit
failure carrying the retry-after. The final failure then reaches the
application executor, which applies model-level ``MODEL_FALLBACKS``.
"""

import asyncio
import json
import math
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass

from loguru import logger
from loguru._logger import context as _loguru_context

from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.config.provider_keys import ProviderKey
from free_claude_code.core.anthropic.models import MessagesRequest
from free_claude_code.core.async_iterators import try_close_async_iterator
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.json_types import JsonObject, JsonValue
from free_claude_code.core.openai_responses import OpenAIResponsesRequest
from free_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from free_claude_code.core.storage import Store
from free_claude_code.core.trace import trace_event

from .admission import _retry_after_seconds
from .base import BaseProvider, ProviderConfig
from .endpoint_health import (
    CircuitState,
    Clock,
    EndpointHealth,
    EndpointHealthTracker,
    HealthPolicy,
    HealthSignal,
)
from .failure_policy import transient_error_text, underlying_provider_error
from .usage_records import UsageRecord, UsageRecorder

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_MAX_WAIT_S = 60.0

Sleep = Callable[[float], Awaitable[None]]
StreamOpener = Callable[[BaseProvider], AsyncIterator[str]]

_QUOTA_MARKERS = ("quota", "insufficient_quota", "credits", "billing")
_OUTCOMES = {
    HealthSignal.RATE_LIMITED: "rate_limited",
    HealthSignal.QUOTA_EXHAUSTED: "quota_exhausted",
    HealthSignal.AUTH_FAILED: "auth_failed",
    HealthSignal.TRANSIENT: "failed",
    HealthSignal.NO_IMPACT: "rejected",
}
_CIRCUIT_RANK = {
    CircuitState.HEALTHY: 0,
    CircuitState.DEGRADED: 1,
    CircuitState.HALF_OPEN: 2,
    CircuitState.OPEN: 3,
}


def _raw_cause(failure: ExecutionFailure) -> Exception | None:
    cause = failure.__cause__
    return underlying_provider_error(cause) if isinstance(cause, Exception) else None


def health_signal(failure: ExecutionFailure) -> HealthSignal:
    """Map the canonical provider failure to its per-key health meaning."""
    kind = failure.kind
    if kind is FailureKind.RATE_LIMIT:
        cause = _raw_cause(failure)
        text = transient_error_text(cause) if cause is not None else ""
        if any(marker in text for marker in _QUOTA_MARKERS):
            return HealthSignal.QUOTA_EXHAUSTED
        return HealthSignal.RATE_LIMITED
    if kind is FailureKind.PERMISSION:
        if failure.status_code == 402:
            return HealthSignal.QUOTA_EXHAUSTED
        return HealthSignal.AUTH_FAILED
    if kind is FailureKind.AUTHENTICATION:
        return HealthSignal.AUTH_FAILED
    if kind in (FailureKind.OVERLOADED, FailureKind.TIMEOUT, FailureKind.UNAVAILABLE):
        return HealthSignal.TRANSIENT
    if kind is FailureKind.UPSTREAM and failure.retryable:
        return HealthSignal.TRANSIENT
    return HealthSignal.NO_IMPACT


def failure_retry_after(failure: ExecutionFailure) -> float | None:
    """Provider-sent Retry-After seconds retained on the failure's raw cause."""
    cause = _raw_cause(failure)
    return _retry_after_seconds(cause) if cause is not None else None


def current_claude_session_id() -> str | None:
    """Claude session id bound by the API request middleware, if any."""
    # ponytail: reads loguru's contextualize() vars (the request middleware binds
    # claude_session_id there); pass it explicitly if providers ever get a request ctx.
    value = _loguru_context.get().get("claude_session_id")
    return value if isinstance(value, str) and value else None


class _UsageSniffer:
    """Pick token usage out of Anthropic / Responses SSE chunks."""

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None

    def feed(self, chunk: str) -> None:
        if '"usage"' not in chunk:
            return
        for line in chunk.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line[5:])
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            for holder in (payload, payload.get("message"), payload.get("response")):
                usage = holder.get("usage") if isinstance(holder, dict) else None
                if isinstance(usage, dict):
                    self._take(usage)

    def _take(self, usage: dict[str, JsonValue]) -> None:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int) and input_tokens > 0:
            self.input_tokens = input_tokens
        if isinstance(output_tokens, int) and output_tokens > 0:
            self.output_tokens = output_tokens


class EndpointPool:
    """Process-wide key health and usage; outlives provider generations."""

    def __init__(
        self,
        store: Store,
        *,
        policy: HealthPolicy | None = None,
        clock: Clock = time.time,
        sleep: Sleep = asyncio.sleep,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_wait_s: float = DEFAULT_MAX_WAIT_S,
    ) -> None:
        self.health = EndpointHealthTracker(store, policy=policy, clock=clock)
        self.usage = UsageRecorder(store, clock=clock)
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.max_wait_s = max_wait_s
        self._configured: set[tuple[str, str]] = set()

    def register(self, provider_id: str, key: ProviderKey) -> EndpointHealth:
        self.health.register(provider_id, key.label, key.fingerprint)
        self._configured.add((provider_id, key.label))
        return self.endpoint(provider_id, key.label)

    def endpoint(self, provider_id: str, label: str) -> EndpointHealth:
        health = self.health.get(provider_id, label)
        if health is None:
            raise AssertionError(f"endpoint {provider_id}/{label} is not registered")
        return health

    def record_usage(self, record: UsageRecord) -> None:
        try:
            self.usage.record(record)
        except Exception as exc:
            logger.warning("Usage record write failed: exc_type={}", type(exc).__name__)

    def endpoint_health(self) -> JsonObject:
        endpoints: list[JsonValue] = []
        for health in self.health.endpoints():
            snapshot = self.health.snapshot(health)
            snapshot["configured"] = (
                health.provider_id,
                health.label,
            ) in self._configured
            endpoints.append(snapshot)
        return {"generated_at": self.health.now(), "endpoints": endpoints}

    def usage_summary(self, minutes: float, *, limit: int = 50) -> JsonObject:
        aggregates: list[JsonValue] = list(self.usage.endpoint_aggregates(minutes))
        sessions: list[JsonValue] = list(self.usage.session_totals(minutes))
        recent: list[JsonValue] = list(self.usage.recent(limit))
        return {
            "minutes": minutes,
            "generated_at": self.health.now(),
            "endpoints": aggregates,
            "sessions": sessions,
            "recent": recent,
        }

    def reset_endpoint(self, provider_id: str, label: str) -> bool:
        return self.health.reset(provider_id, label)


@dataclass(frozen=True, slots=True)
class PoolMember:
    key: ProviderKey
    provider: BaseProvider


class PooledProvider(BaseProvider):
    """One logical provider backed by several keys with health-aware failover."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        provider_id: str,
        members: Sequence[PoolMember],
        pool: EndpointPool,
    ) -> None:
        super().__init__(config)
        if not members:
            raise ValueError("PooledProvider requires at least one key")
        self._provider_id = provider_id
        self._members = tuple(members)
        self._pool = pool
        self._max_concurrency = config.max_concurrency
        for member in self._members:
            pool.register(provider_id, member.key)

    @property
    def members(self) -> tuple[PoolMember, ...]:
        return self._members

    def preflight_messages(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        self._members[0].provider.preflight_messages(request, reasoning=reasoning)

    def preflight_responses(
        self,
        request: OpenAIResponsesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        self._members[0].provider.preflight_responses(request, reasoning=reasoning)

    async def cleanup(self) -> None:
        errors: list[Exception] = []
        for member in self._members:
            try:
                await member.provider.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("One or more pooled provider cleanups failed", errors)

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        """Ask keys in preference order; the first key that answers wins."""
        last_error: Exception | None = None
        for member, _health in self._ranked(self._members):
            try:
                return await member.provider.list_model_infos()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
        if last_error is None:
            raise AssertionError("pooled provider has no members")
        raise last_error

    def stream_messages(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        response_model: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._stream(
            lambda provider: provider.stream_messages(
                request,
                input_tokens,
                request_id=request_id,
                response_model=response_model,
                reasoning=reasoning,
            ),
            request_id=request_id,
            model=request.model,
        )

    def stream_responses(
        self,
        request: OpenAIResponsesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        response_model: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._stream(
            lambda provider: provider.stream_responses(
                request,
                input_tokens,
                request_id=request_id,
                response_model=response_model,
                reasoning=reasoning,
            ),
            request_id=request_id,
            model=request.model,
        )

    def _ranked(
        self, members: Sequence[PoolMember]
    ) -> list[tuple[PoolMember, EndpointHealth]]:
        pairs = [
            (index, member, self._pool.endpoint(self._provider_id, member.key.label))
            for index, member in enumerate(members)
        ]
        pairs.sort(
            key=lambda item: (
                _CIRCUIT_RANK[item[2].circuit],
                item[2].consecutive_failures,
                item[2].in_flight,
                item[2].last_selected,
                item[0],
            )
        )
        return [(member, health) for _index, member, health in pairs]

    async def _acquire(
        self,
        tried: set[str],
        deadline: float,
        last_failure: ExecutionFailure | None,
    ) -> tuple[PoolMember, EndpointHealth]:
        tracker = self._pool.health
        limit = self._max_concurrency
        while True:
            untried = [m for m in self._members if m.key.label not in tried]
            for candidates in (untried, self._members):
                for member, health in self._ranked(candidates):
                    if tracker.try_acquire(health, limit):
                        return member, health
            now = tracker.now()
            available_at = tracker.earliest_availability(
                (health for _member, health in self._ranked(self._members)), limit
            )
            if available_at is None:
                raise last_failure or self._all_disabled_failure()
            if available_at > deadline:
                raise self._capacity_failure(available_at - now)
            trace_event(
                stage="provider",
                event="provider.key_pool.waiting",
                source="provider",
                provider=self._provider_id,
                wait_s=round(available_at - now, 3),
            )
            await self._pool.sleep(max(0.0, available_at - now))

    def _capacity_failure(self, retry_after_s: float) -> ExecutionFailure:
        seconds = max(1, math.ceil(retry_after_s))
        return ExecutionFailure(
            kind=FailureKind.RATE_LIMIT,
            status_code=429,
            message=(
                f"All {len(self._members)} {self._provider_id} API keys are rate "
                f"limited, cooling down, or at capacity. Retry after {seconds}s."
            ),
            retryable=True,
        )

    def _all_disabled_failure(self) -> ExecutionFailure:
        return ExecutionFailure(
            kind=FailureKind.AUTHENTICATION,
            status_code=401,
            message=(
                f"All {self._provider_id} API keys are disabled after authentication "
                "failures. Fix the keys or reset them via "
                f"POST /admin/api/endpoints/{self._provider_id}/<label>/reset."
            ),
            retryable=False,
        )

    async def _stream(
        self,
        open_stream: StreamOpener,
        *,
        request_id: str | None,
        model: str,
    ) -> AsyncIterator[str]:
        tracker = self._pool.health
        session_id = current_claude_session_id()
        # ponytail: one key has no failover target; its inner admission already
        # retries transient errors, so the pool adds health gating only.
        max_attempts = 1 if len(self._members) == 1 else self._pool.max_attempts
        deadline = tracker.now() + self._pool.max_wait_s
        tried: set[str] = set()
        failover_from: str | None = None
        last_failure: ExecutionFailure | None = None
        for attempt in range(1, max_attempts + 1):
            member, health = await self._acquire(tried, deadline, last_failure)
            label = member.key.label
            started = tracker.now()
            sniffer = _UsageSniffer()
            committed = False
            succeeded = False
            failure: ExecutionFailure | None = None
            stream: AsyncIterator[str] | None = None
            try:
                stream = open_stream(member.provider)
                async for chunk in stream:
                    if chunk:
                        committed = True
                        sniffer.feed(chunk)
                    yield chunk
                succeeded = True
            except ExecutionFailure as exc:
                failure = exc
            finally:
                active = sys.exception()
                if stream is not None:
                    await try_close_async_iterator(stream)
                latency_ms = (tracker.now() - started) * 1000
                signal: HealthSignal | None = None
                if succeeded:
                    tracker.record_success(health, latency_ms)
                    outcome = "ok"
                elif failure is not None:
                    signal = health_signal(failure)
                    tracker.record_failure(
                        health, signal, retry_after_s=failure_retry_after(failure)
                    )
                    outcome = _OUTCOMES[signal]
                elif isinstance(active, asyncio.CancelledError | GeneratorExit):
                    outcome = "cancelled"
                else:
                    outcome = "failed"
                tracker.release(health)
                self._pool.record_usage(
                    UsageRecord(
                        provider_id=self._provider_id,
                        key_label=label,
                        model=model,
                        request_id=request_id,
                        claude_session_id=session_id,
                        input_tokens=sniffer.input_tokens,
                        output_tokens=sniffer.output_tokens,
                        latency_ms=latency_ms,
                        outcome=outcome,
                        attempt=attempt,
                        failure_kind=failure.kind.value if failure else None,
                        failover_from=failover_from,
                    )
                )
            if failure is None:
                return
            if committed or signal is HealthSignal.NO_IMPACT:
                raise failure
            last_failure = failure
            tried.add(label)
            failover_from = label
            if attempt == max_attempts:
                break
            trace_event(
                stage="provider",
                event="provider.key_pool.failover",
                source="provider",
                provider=self._provider_id,
                request_id=request_id,
                from_label=label,
                attempt=attempt,
                max_attempts=max_attempts,
                failure_kind=failure.kind.value,
                health_signal=signal.value if signal else None,
            )
            logger.warning(
                "Key pool failover: provider={} from={} attempt={}/{} failure_kind={}",
                self._provider_id,
                label,
                attempt,
                max_attempts,
                failure.kind.value,
            )
        if last_failure is None:
            raise AssertionError("key pool exhausted without a failure")
        raise last_failure
