"""Multi-key endpoint pool: selection, bounded failover, waiting, usage rows."""

import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator

import pytest
from loguru import logger

from free_claude_code.application.execution import ProviderExecutor
from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.application.routing import (
    ProviderModelTarget,
    ResolvedModelRoute,
    RoutedMessagesRequest,
)
from free_claude_code.config.provider_keys import ProviderKey
from free_claude_code.config.reasoning import ReasoningPreference
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic.models import Message, MessagesRequest
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.openai_responses import OpenAIResponsesRequest
from free_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from free_claude_code.core.request_context import bind_claude_session_id
from free_claude_code.core.storage import Store
from free_claude_code.providers.base import BaseProvider
from free_claude_code.providers.endpoint_health import CircuitState, HealthSignal
from free_claude_code.providers.key_pool import (
    EndpointPool,
    PooledProvider,
    PoolMember,
    health_signal,
)
from free_claude_code.providers.nvidia_nim import NvidiaNimProvider
from free_claude_code.providers.runtime.factory import create_provider
from tests.providers.support import make_provider_config

CANARY = "nvapi-CANARY-4f1d2e9b-never-log-me"

Step = Callable[[], AsyncIterator[str]]


class FakeClock:
    def __init__(self) -> None:
        self.now = 5_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Response:
    def __init__(self, headers: dict[str, str], text: str = "") -> None:
        self.headers = headers
        self.text = text


class UpstreamError(Exception):
    """Raw SDK-like error retained as the canonical failure's cause."""

    def __init__(self, headers: dict[str, str], text: str = "") -> None:
        super().__init__(text or "upstream error")
        self.response = _Response(headers, text)


def failure(
    kind: FailureKind, status: int = 500, *, retryable: bool = True
) -> ExecutionFailure:
    return ExecutionFailure(
        kind=kind,
        status_code=status,
        message=f"{kind.value} failure",
        retryable=retryable,
    )


def fail(exc: ExecutionFailure, cause: Exception | None = None) -> Step:
    async def gen() -> AsyncIterator[str]:
        if cause is not None:
            raise exc from cause
        raise exc
        yield ""

    return gen


def ok(*chunks: str) -> Step:
    async def gen() -> AsyncIterator[str]:
        for chunk in chunks:
            yield chunk

    return gen


def fail_after(exc: ExecutionFailure, *chunks: str) -> Step:
    async def gen() -> AsyncIterator[str]:
        for chunk in chunks:
            yield chunk
        raise exc

    return gen


class ScriptedProvider(BaseProvider):
    def __init__(
        self, *steps: Step, models: frozenset[ProviderModelInfo] | None = None
    ):
        super().__init__(make_provider_config(api_key=CANARY, base_url="http://x"))
        self.steps = list(steps)
        self.calls = 0
        self.models = models
        self.cleaned = False

    def preflight_messages(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        return None

    def preflight_responses(
        self,
        request: OpenAIResponsesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        return None

    async def cleanup(self) -> None:
        self.cleaned = True

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        if self.models is None:
            raise failure(FailureKind.AUTHENTICATION, 401, retryable=False)
        return self.models

    def stream_messages(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        response_model: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        self.calls += 1
        return self.steps.pop(0)()

    def stream_responses(
        self,
        request: OpenAIResponsesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        response_model: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        self.calls += 1
        return self.steps.pop(0)()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def pool(tmp_path, clock: FakeClock) -> Iterator[EndpointPool]:
    store = Store(tmp_path / "fcc.db")
    yield EndpointPool(store, clock=clock, sleep=clock.sleep)
    store.close()


def pooled(
    pool: EndpointPool, *providers: tuple[str, BaseProvider], max_concurrency: int = 5
) -> PooledProvider:
    config = make_provider_config(
        api_key=CANARY, base_url="http://x", max_concurrency=max_concurrency
    )
    members = [
        PoolMember(key=ProviderKey(label=label, key=f"{CANARY}-{label}"), provider=p)
        for label, p in providers
    ]
    return PooledProvider(config, provider_id="nvidia_nim", members=members, pool=pool)


def request() -> MessagesRequest:
    return MessagesRequest(model="m1", messages=[Message(role="user", content="hi")])


async def collect(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


def rows(pool: EndpointPool) -> list[dict]:
    return list(reversed(pool.usage.recent(100)))


def state(pool: EndpointPool, label: str) -> CircuitState:
    return pool.endpoint("nvidia_nim", label).circuit


@pytest.mark.asyncio
async def test_rate_limit_fails_over_and_records_usage_per_attempt(pool, clock):
    cause = UpstreamError({"retry-after": "7"})
    bad = ScriptedProvider(fail(failure(FailureKind.RATE_LIMIT, 429), cause))
    good = ScriptedProvider(ok("event: a\n\n"))
    provider = pooled(pool, ("bad", bad), ("good", good))

    chunks = await collect(provider.stream_messages(request(), request_id="req_1"))

    assert chunks == ["event: a\n\n"]
    assert state(pool, "bad") is CircuitState.DEGRADED
    assert pool.endpoint("nvidia_nim", "bad").cooldown_until == clock.now + 7
    assert state(pool, "good") is CircuitState.HEALTHY
    first, second = rows(pool)
    assert (first["key_label"], first["outcome"], first["attempt"]) == (
        "bad",
        "rate_limited",
        1,
    )
    assert first["failover_from"] is None
    assert first["failure_kind"] == "rate_limit"
    assert (second["key_label"], second["outcome"], second["attempt"]) == (
        "good",
        "ok",
        2,
    )
    assert second["failover_from"] == "bad"
    assert second["request_id"] == "req_1" and second["model"] == "m1"


@pytest.mark.asyncio
async def test_auth_failure_opens_key_and_later_requests_skip_it(pool):
    bad = ScriptedProvider(
        fail(failure(FailureKind.AUTHENTICATION, 401, retryable=False))
    )
    good = ScriptedProvider(ok("x"), ok("y"))
    provider = pooled(pool, ("bad", bad), ("good", good))

    assert await collect(provider.stream_messages(request())) == ["x"]
    assert state(pool, "bad") is CircuitState.OPEN
    assert await collect(provider.stream_messages(request())) == ["y"]
    assert bad.calls == 1 and good.calls == 2
    assert [row["outcome"] for row in rows(pool)] == ["auth_failed", "ok", "ok"]


@pytest.mark.asyncio
async def test_no_failover_after_first_content_chunk(pool):
    first = ScriptedProvider(fail_after(failure(FailureKind.TIMEOUT), "partial"))
    second = ScriptedProvider(ok("never"))
    provider = pooled(pool, ("a", first), ("b", second))

    stream = provider.stream_messages(request())
    assert await anext(stream) == "partial"
    with pytest.raises(ExecutionFailure) as caught:
        await anext(stream)

    assert caught.value.kind is FailureKind.TIMEOUT
    assert second.calls == 0
    assert [row["outcome"] for row in rows(pool)] == ["failed"]
    assert pool.endpoint("nvidia_nim", "a").consecutive_failures == 1


@pytest.mark.asyncio
async def test_request_errors_do_not_fail_over_or_touch_health(pool):
    first = ScriptedProvider(
        fail(failure(FailureKind.INVALID_REQUEST, 400, retryable=False))
    )
    second = ScriptedProvider(ok("never"))
    provider = pooled(pool, ("a", first), ("b", second))

    with pytest.raises(ExecutionFailure):
        await collect(provider.stream_messages(request()))

    assert second.calls == 0
    assert state(pool, "a") is CircuitState.HEALTHY
    assert rows(pool)[0]["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_failover_is_bounded_to_four_attempts(pool):
    providers = [
        (f"k{i}", ScriptedProvider(fail(failure(FailureKind.OVERLOADED, 529))))
        for i in range(5)
    ]
    provider = pooled(pool, *providers)

    with pytest.raises(ExecutionFailure) as caught:
        await collect(provider.stream_messages(request()))

    assert caught.value.kind is FailureKind.OVERLOADED
    assert sum(p.calls for _label, p in providers) == 4
    recorded = rows(pool)
    assert [row["attempt"] for row in recorded] == [1, 2, 3, 4]
    assert [row["failover_from"] for row in recorded] == [None, "k0", "k1", "k2"]


@pytest.mark.asyncio
async def test_retries_previously_tried_key_once_all_keys_were_tried(pool):
    a = ScriptedProvider(fail(failure(FailureKind.TIMEOUT)), ok("second-try"))
    b = ScriptedProvider(fail(failure(FailureKind.UNAVAILABLE)))
    provider = pooled(pool, ("a", a), ("b", b))

    assert await collect(provider.stream_messages(request())) == ["second-try"]
    assert [row["key_label"] for row in rows(pool)] == ["a", "b", "a"]


@pytest.mark.asyncio
async def test_waits_for_earliest_cooldown_then_succeeds(pool, clock):
    a = ScriptedProvider(
        fail(
            failure(FailureKind.RATE_LIMIT, 429), UpstreamError({"retry-after": "12"})
        ),
        ok("late"),
    )
    b = ScriptedProvider(
        fail(failure(FailureKind.RATE_LIMIT, 429), UpstreamError({"retry-after": "30"}))
    )
    provider = pooled(pool, ("a", a), ("b", b))

    assert await collect(provider.stream_messages(request())) == ["late"]
    assert clock.sleeps == [pytest.approx(12)]
    assert [row["outcome"] for row in rows(pool)] == [
        "rate_limited",
        "rate_limited",
        "ok",
    ]


@pytest.mark.asyncio
async def test_fails_with_retry_after_when_wait_exceeds_sixty_seconds(pool, clock):
    only = ScriptedProvider(ok("never"))
    other = ScriptedProvider(ok("never"))
    provider = pooled(pool, ("a", only), ("b", other))
    for label in ("a", "b"):
        pool.health.record_failure(
            pool.endpoint("nvidia_nim", label),
            HealthSignal.RATE_LIMITED,
            retry_after_s=90,
        )

    with pytest.raises(ExecutionFailure) as caught:
        await collect(provider.stream_messages(request()))

    assert caught.value.kind is FailureKind.RATE_LIMIT
    assert caught.value.status_code == 429
    assert "Retry after 90s" in caught.value.message
    assert clock.sleeps == []
    assert only.calls == other.calls == 0


@pytest.mark.asyncio
async def test_all_keys_auth_disabled_fails_without_calling_upstream(pool):
    a = ScriptedProvider(ok("never"))
    provider = pooled(pool, ("a", a), ("b", ScriptedProvider(ok("never"))))
    for label in ("a", "b"):
        pool.health.record_failure(
            pool.endpoint("nvidia_nim", label), HealthSignal.AUTH_FAILED
        )

    with pytest.raises(ExecutionFailure) as caught:
        await collect(provider.stream_messages(request()))

    assert caught.value.kind is FailureKind.AUTHENTICATION
    assert a.calls == 0
    assert pool.reset_endpoint("nvidia_nim", "a")
    a.steps[:] = [ok("after-reset")]
    assert await collect(provider.stream_messages(request())) == ["after-reset"]


@pytest.mark.asyncio
async def test_single_key_makes_exactly_one_attempt(pool):
    only = ScriptedProvider(fail(failure(FailureKind.TIMEOUT)), ok("unused"))
    provider = pooled(pool, ("primary", only))

    with pytest.raises(ExecutionFailure):
        await collect(provider.stream_messages(request()))
    assert only.calls == 1


@pytest.mark.asyncio
async def test_round_robin_between_equally_healthy_keys(pool):
    a = ScriptedProvider(ok("a1"), ok("a2"))
    b = ScriptedProvider(ok("b1"), ok("b2"))
    provider = pooled(pool, ("a", a), ("b", b))

    outputs = [await collect(provider.stream_messages(request())) for _ in range(4)]

    assert outputs == [["a1"], ["b1"], ["a2"], ["b2"]]


@pytest.mark.asyncio
async def test_healthiest_key_is_preferred(pool):
    a = ScriptedProvider(ok("unused"))
    b = ScriptedProvider(ok("b"))
    provider = pooled(pool, ("a", a), ("b", b))
    pool.health.record_failure(pool.endpoint("nvidia_nim", "a"), HealthSignal.TRANSIENT)

    assert await collect(provider.stream_messages(request())) == ["b"]
    assert a.calls == 0


@pytest.mark.asyncio
async def test_key_at_concurrency_limit_is_skipped(pool):
    a = ScriptedProvider(ok("a"))
    b = ScriptedProvider(ok("b"))
    provider = pooled(pool, ("a", a), ("b", b), max_concurrency=1)
    held = pool.endpoint("nvidia_nim", "a")
    assert pool.health.try_acquire(held, 1)

    assert await collect(provider.stream_messages(request())) == ["b"]
    pool.health.release(held)


@pytest.mark.asyncio
async def test_tokens_and_session_are_recorded(pool):
    start = (
        'event: message_start\ndata: {"message": {"usage": {"input_tokens": 42}}}\n\n'
    )
    delta = 'event: message_delta\ndata: {"usage": {"output_tokens": 7}}\n\n'
    provider = pooled(pool, ("a", ScriptedProvider(ok(start, delta))))

    with bind_claude_session_id("sess-123"):
        await collect(provider.stream_messages(request()))

    row = rows(pool)[0]
    assert (row["input_tokens"], row["output_tokens"]) == (42, 7)
    assert row["claude_session_id"] == "sess-123"
    [session] = pool.usage.session_totals()
    assert session["claude_session_id"] == "sess-123"
    assert (session["input_tokens"], session["output_tokens"]) == (42, 7)


@pytest.mark.asyncio
async def test_responses_stream_fails_over_and_reads_usage(pool):
    done = (
        "event: response.completed\n"
        'data: {"response": {"usage": {"input_tokens": 3, "output_tokens": 4}}}\n\n'
    )
    bad = ScriptedProvider(fail(failure(FailureKind.UNAVAILABLE, 503)))
    good = ScriptedProvider(ok(done))
    provider = pooled(pool, ("bad", bad), ("good", good))
    req = OpenAIResponsesRequest(model="m2", input="hi")

    assert await collect(provider.stream_responses(req)) == [done]
    row = rows(pool)[-1]
    assert (row["model"], row["input_tokens"], row["output_tokens"]) == ("m2", 3, 4)
    assert row["failover_from"] == "bad"


@pytest.mark.asyncio
async def test_consumer_close_records_cancelled_and_releases(pool):
    provider = pooled(pool, ("a", ScriptedProvider(ok("1", "2", "3"))))
    stream = provider.stream_messages(request())
    assert await anext(stream) == "1"
    assert isinstance(stream, AsyncGenerator)
    await stream.aclose()

    assert rows(pool)[0]["outcome"] == "cancelled"
    assert pool.endpoint("nvidia_nim", "a").in_flight == 0


@pytest.mark.asyncio
async def test_model_listing_uses_first_answering_key_and_cleanup_all(pool):
    models = frozenset({ProviderModelInfo("m")})
    bad = ScriptedProvider()
    good = ScriptedProvider(models=models)
    provider = pooled(pool, ("bad", bad), ("good", good))

    assert await provider.list_model_infos() == models
    await provider.cleanup()
    assert bad.cleaned and good.cleaned


@pytest.mark.asyncio
async def test_key_failover_runs_before_model_fallback(pool):
    limited = UpstreamError({"retry-after": "120"})
    exhausted = pooled(
        pool,
        ("a", ScriptedProvider(fail(failure(FailureKind.RATE_LIMIT, 429), limited))),
        (
            "b",
            ScriptedProvider(
                fail(failure(FailureKind.AUTHENTICATION, 401, retryable=False))
            ),
        ),
    )
    fallback = ScriptedProvider(ok("from-fallback-model"))
    providers: dict[str, BaseProvider] = {"nvidia_nim": exhausted, "other": fallback}
    executor = ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 1,
        progress_timeout_seconds=60.0,
    )
    routed = RoutedMessagesRequest(
        request=request(),
        resolved=ResolvedModelRoute(
            original_model="gateway",
            primary=ProviderModelTarget("nvidia_nim", "m1", "nvidia_nim/m1"),
            fallbacks=(ProviderModelTarget("other", "m2", "other/m2"),),
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
    )

    chunks = await collect(
        executor.stream_messages(routed, raw_log_payload={}, request_id="req_x")
    )

    assert chunks == ["from-fallback-model"]
    assert [row["key_label"] for row in rows(pool)] == ["a", "b"]


@pytest.mark.parametrize(
    ("exc", "cause", "expected"),
    [
        (failure(FailureKind.RATE_LIMIT, 429), None, HealthSignal.RATE_LIMITED),
        (
            failure(FailureKind.RATE_LIMIT, 429),
            UpstreamError({}, "You exceeded your current quota"),
            HealthSignal.QUOTA_EXHAUSTED,
        ),
        (failure(FailureKind.PERMISSION, 402), None, HealthSignal.QUOTA_EXHAUSTED),
        (failure(FailureKind.PERMISSION, 403), None, HealthSignal.AUTH_FAILED),
        (failure(FailureKind.AUTHENTICATION, 401), None, HealthSignal.AUTH_FAILED),
        (failure(FailureKind.OVERLOADED, 529), None, HealthSignal.TRANSIENT),
        (failure(FailureKind.TIMEOUT), None, HealthSignal.TRANSIENT),
        (failure(FailureKind.UNAVAILABLE), None, HealthSignal.TRANSIENT),
        (failure(FailureKind.UPSTREAM), None, HealthSignal.TRANSIENT),
        (
            failure(FailureKind.UPSTREAM, 404, retryable=False),
            None,
            HealthSignal.NO_IMPACT,
        ),
        (failure(FailureKind.INVALID_REQUEST, 400), None, HealthSignal.NO_IMPACT),
        (
            failure(FailureKind.CONTEXT_WINDOW_EXCEEDED, 400),
            None,
            HealthSignal.NO_IMPACT,
        ),
    ],
)
def test_health_signal_mapping(exc, cause, expected) -> None:
    if cause is not None:
        exc.__cause__ = cause
    assert health_signal(exc) is expected


def test_factory_builds_one_single_attempt_client_per_pooled_key(pool) -> None:
    settings = Settings.model_validate(
        {
            "MODEL": "nvidia_nim/m",
            "NVIDIA_NIM_API_KEY": f"{CANARY}-single",
            "NVIDIA_NIM_API_KEYS": f"a={CANARY}-a,b={CANARY}-b",
        }
    )
    provider = create_provider("nvidia_nim", settings, endpoint_pool=pool)

    assert isinstance(provider, PooledProvider)
    assert [m.key.label for m in provider.members] == ["a", "b", "primary"]
    for member in provider.members:
        assert isinstance(member.provider, NvidiaNimProvider)
        assert member.provider._admission._max_attempts == 1
    assert [m.provider._config.api_key for m in provider.members] == [
        f"{CANARY}-a",
        f"{CANARY}-b",
        f"{CANARY}-single",
    ]


def test_factory_without_pool_keeps_plain_provider(pool) -> None:
    settings = Settings.model_validate(
        {"MODEL": "nvidia_nim/m", "NVIDIA_NIM_API_KEYS": f"{CANARY}-a"}
    )
    assert isinstance(create_provider("nvidia_nim", settings), NvidiaNimProvider)
    single = create_provider("nvidia_nim", settings, endpoint_pool=pool)
    assert isinstance(single, PooledProvider)
    inner = single.members[0].provider
    assert isinstance(inner, NvidiaNimProvider)
    assert inner._admission._max_attempts == 5


@pytest.mark.asyncio
async def test_keys_never_leak_into_logs_usage_or_health(pool):
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="DEBUG")
    try:
        cause = UpstreamError({"retry-after": "1"})
        provider = pooled(
            pool,
            (
                "bad",
                ScriptedProvider(fail(failure(FailureKind.RATE_LIMIT, 429), cause)),
            ),
            ("worse", ScriptedProvider(fail(failure(FailureKind.AUTHENTICATION, 401)))),
            ("good", ScriptedProvider(ok("x"))),
        )
        await collect(provider.stream_messages(request(), request_id="req_c"))
    finally:
        logger.remove(sink)

    dumped = json.dumps(
        [pool.endpoint_health(), pool.usage_summary(60), repr(provider.members)],
        default=str,
    )
    assert "CANARY" not in dumped
    assert messages and not any("CANARY" in line for line in messages)
    assert any("key_pool.failover" in line for line in messages)
