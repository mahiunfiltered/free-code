"""First-byte and idle stream timeouts against a real local stub HTTP server."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace

import pytest

from free_claude_code.config.nim import NimSettings
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.provider_keys import ProviderKey
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic.models import Message, MessagesRequest
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.storage import Store
from free_claude_code.providers.endpoint_health import HealthSignal
from free_claude_code.providers.failure_policy import (
    ProviderStreamStalled,
    classify_provider_failure,
)
from free_claude_code.providers.http import StallGuardedStream, open_guarded_stream
from free_claude_code.providers.key_pool import (
    EndpointPool,
    PooledProvider,
    PoolMember,
    health_signal,
)
from free_claude_code.providers.nvidia_nim import NvidiaNimProvider
from free_claude_code.providers.runtime.config import build_provider_config
from tests.providers.support import immediate_admission, make_provider_config

Behaviour = Callable[[asyncio.StreamWriter], Awaitable[None]]


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _chunk(text: str, finish: str | None = None) -> dict:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }


_HEADERS = (
    b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
    b"cache-control: no-cache\r\nconnection: close\r\n\r\n"
)


async def _hang(writer: asyncio.StreamWriter) -> None:
    await asyncio.sleep(3600)


async def _keepalive_only(writer: asyncio.StreamWriter) -> None:
    """Headers plus SSE comments forever: defeats a socket read timeout."""
    writer.write(_HEADERS)
    while True:
        writer.write(b": ping\n\n")
        await writer.drain()
        await asyncio.sleep(0.02)


async def _one_chunk_then_keepalive(writer: asyncio.StreamWriter) -> None:
    writer.write(_HEADERS + _sse(_chunk("partial")))
    await writer.drain()
    while True:
        writer.write(b": ping\n\n")
        await writer.drain()
        await asyncio.sleep(0.02)


async def _healthy(writer: asyncio.StreamWriter) -> None:
    writer.write(
        _HEADERS
        + _sse(_chunk("hello"))
        + _sse(_chunk(" world", "stop"))
        + b"data: [DONE]\n\n"
    )
    await writer.drain()


class StubServer:
    """Minimal HTTP/1.1 server choosing a behaviour by bearer token."""

    def __init__(self, behaviours: dict[str, Behaviour]) -> None:
        self._behaviours = behaviours
        self.requests: list[str] = []
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/v1"

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for task in self._handlers:
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            length = 0
            token = ""
            for line in lines[1:]:
                name, _, value = line.partition(":")
                if name.lower() == "content-length":
                    length = int(value)
                if name.lower() == "authorization":
                    token = value.strip().removeprefix("Bearer ")
            await reader.readexactly(length)
            self.requests.append(token)
            await self._behaviours[token](writer)
        except asyncio.CancelledError, ConnectionError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def _request() -> MessagesRequest:
    return MessagesRequest(
        model="m", max_tokens=64, messages=[Message(role="user", content="hi")]
    )


def _nim(base_url: str, key: str, *, first_byte: float, idle: float):
    config = replace(
        make_provider_config(api_key=key, base_url=base_url),
        http_first_byte_timeout=first_byte,
        http_stream_idle_timeout=idle,
    )
    return NvidiaNimProvider(
        config,
        nim_settings=NimSettings(),
        admission=immediate_admission(max_attempts=1),
    )


async def _collect(stream: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in stream])


@pytest.mark.asyncio
async def test_first_byte_stall_fails_over_to_next_key(tmp_path) -> None:
    server = StubServer({"key-a": _hang, "key-b": _healthy})
    store = Store(tmp_path / "fcc.db")
    try:
        async with server as base_url:
            pool = EndpointPool(store)
            provider = PooledProvider(
                make_provider_config(api_key="x", base_url=base_url),
                provider_id="nvidia_nim",
                members=[
                    PoolMember(
                        ProviderKey("a", "key-a"),
                        _nim(base_url, "key-a", first_byte=0.3, idle=5),
                    ),
                    PoolMember(
                        ProviderKey("b", "key-b"),
                        _nim(base_url, "key-b", first_byte=5, idle=5),
                    ),
                ],
                pool=pool,
            )
            body = await asyncio.wait_for(
                _collect(provider.stream_messages(_request(), request_id="r1")),
                timeout=10,
            )
            await provider.cleanup()
        assert "hello" in body and "world" in body
        rows = list(reversed(pool.usage.recent(10)))
        assert [(r["key_label"], r["outcome"], r["failure_kind"]) for r in rows] == [
            ("a", "failed", "timeout"),
            ("b", "ok", None),
        ]
        latency = rows[0]["latency_ms"]
        assert isinstance(latency, float) and latency < 3000
        assert rows[1]["failover_from"] == "a"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_keepalive_comments_do_not_defeat_first_byte_timeout() -> None:
    server = StubServer({"key-a": _keepalive_only})
    async with server as base_url:
        provider = _nim(base_url, "key-a", first_byte=0.3, idle=5)
        with pytest.raises(ExecutionFailure) as caught:
            await asyncio.wait_for(
                _collect(provider.stream_messages(_request(), request_id="r2")),
                timeout=10,
            )
        await provider.cleanup()
    assert caught.value.kind is FailureKind.TIMEOUT
    assert caught.value.retryable is True
    assert health_signal(caught.value) is HealthSignal.TRANSIENT


@pytest.mark.asyncio
async def test_mid_stream_stall_hits_idle_timeout() -> None:
    server = StubServer({"key-a": _one_chunk_then_keepalive})
    async with server as base_url:
        provider = _nim(base_url, "key-a", first_byte=5, idle=0.3)
        started = asyncio.get_running_loop().time()
        with pytest.raises(ExecutionFailure) as caught:
            await asyncio.wait_for(
                _collect(provider.stream_messages(_request(), request_id="r3")),
                timeout=10,
            )
        elapsed = asyncio.get_running_loop().time() - started
        await provider.cleanup()
    assert caught.value.kind is FailureKind.TIMEOUT
    assert elapsed < 5


@pytest.mark.asyncio
async def test_guard_resets_idle_deadline_per_chunk() -> None:
    async def slow_but_steady() -> AsyncIterator[int]:
        for item in range(4):
            await asyncio.sleep(0.1)
            yield item

    stream = await open_guarded_stream(
        lambda: asyncio.sleep(0, result=slow_but_steady()),
        first_byte_timeout_s=0.25,
        idle_timeout_s=0.25,
    )
    assert [item async for item in stream] == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_guard_first_byte_deadline_spans_open_and_first_chunk() -> None:
    async def late_first_chunk() -> AsyncIterator[int]:
        await asyncio.sleep(0.2)
        yield 1

    async def slow_open() -> AsyncIterator[int]:
        await asyncio.sleep(0.2)
        return late_first_chunk()

    stream = await open_guarded_stream(
        slow_open, first_byte_timeout_s=0.3, idle_timeout_s=5
    )
    with pytest.raises(ProviderStreamStalled, match="first-byte"):
        await anext(stream)


@pytest.mark.asyncio
async def test_guard_open_timeout_and_close_delegation() -> None:
    with pytest.raises(ProviderStreamStalled, match=r"0.05s"):
        await open_guarded_stream(
            lambda: asyncio.sleep(1), first_byte_timeout_s=0.05, idle_timeout_s=1
        )

    class SdkLikeStream:
        closed = False

        async def close(self) -> None:
            self.closed = True

    source = SdkLikeStream()
    await StallGuardedStream(
        source, first_byte_deadline=0, first_byte_timeout_s=1, idle_timeout_s=1
    ).aclose()
    assert source.closed


def test_stall_classifies_as_retryable_timeout_with_its_message() -> None:
    failure = classify_provider_failure(
        ProviderStreamStalled.idle(90),
        provider_name="NIM",
        read_timeout_s=120,
        request_id=None,
    )
    assert failure.kind is FailureKind.TIMEOUT
    assert failure.retryable is True
    assert "no chunk for 90s" in failure.message


def test_timeouts_are_settings_driven() -> None:
    settings = Settings.model_validate(
        {
            "MODEL": "nvidia_nim/m",
            "NVIDIA_NIM_API_KEY": "k",
            "HTTP_FIRST_BYTE_TIMEOUT": "240",
            "HTTP_STREAM_IDLE_TIMEOUT": "45",
        }
    )
    config = build_provider_config(PROVIDER_CATALOG["nvidia_nim"], settings)
    assert config.http_first_byte_timeout == 240
    assert config.http_stream_idle_timeout == 45
    defaults = Settings.model_validate({"MODEL": "nvidia_nim/m"})
    assert defaults.http_first_byte_timeout == 120
    assert defaults.http_stream_idle_timeout == 90
