"""Shared HTTP lifecycle helpers for upstream provider clients."""

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Self, TypeVar

import httpx2
from loguru import logger

from free_claude_code.core.trace import trace_event
from free_claude_code.providers.admission import ProviderAttempt
from free_claude_code.providers.failure_policy import ProviderStreamStalled

_ResourceT = TypeVar("_ResourceT")

_SSE_DONE_MARKER = b"[DONE]"
_SSE_DONE_DRAIN_TIMEOUT_S = 0.25


class _ReusableSseStream(httpx2.AsyncByteStream):
    """Read an SSE body's trailing bytes after ``[DONE]`` so its connection is reused.

    The OpenAI SDK stops at ``data: [DONE]`` and closes the response before the
    chunked terminator is read; an unfinished HTTP/1.1 response cannot return
    to the pool, so every request paid a fresh TCP+TLS handshake. Streams closed
    before ``[DONE]`` (cancellation, errors) are closed without draining.
    """

    def __init__(self, inner: httpx2.AsyncByteStream) -> None:
        self._inner = inner
        self._tail = b""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._tail = (self._tail + chunk)[-32:]
            yield chunk

    async def aclose(self) -> None:
        try:
            if _SSE_DONE_MARKER in self._tail:
                async with asyncio.timeout(_SSE_DONE_DRAIN_TIMEOUT_S):
                    async for _ in self._inner:
                        pass
        except Exception:
            pass  # best effort: an unread terminator only costs connection reuse
        finally:
            await self._inner.aclose()


async def reuse_connection_after_sse_done(response: httpx2.Response) -> None:
    """``httpx2`` response hook installing :class:`_ReusableSseStream`."""
    if isinstance(response.stream, httpx2.AsyncByteStream):
        response.stream = _ReusableSseStream(response.stream)


async def maybe_await_aclose(response: Any) -> None:
    """Call ``aclose`` on httpx-like responses; ignore sync test doubles."""
    close = getattr(response, "aclose", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def close_provider_stream(
    stream: Any,
    *,
    active_error: BaseException | None,
    provider_name: str,
    request_id: str | None,
) -> None:
    """Close one stream without letting cleanup change its established outcome."""
    try:
        await maybe_await_aclose(stream)
    except (Exception, asyncio.CancelledError) as close_error:
        if isinstance(close_error, asyncio.CancelledError):
            current_task = asyncio.current_task()
            if (
                isinstance(active_error, asyncio.CancelledError)
                or current_task is None
                or current_task.cancelling()
            ):
                raise
        active_error_type = (
            type(active_error).__name__ if active_error is not None else None
        )
        trace_event(
            stage="provider",
            event="provider.stream.close_failed",
            source="provider",
            provider=provider_name,
            request_id=request_id,
            close_exc_type=type(close_error).__name__,
            preserved_exc_type=active_error_type,
        )
        logger.warning(
            "{}_STREAM_CLOSE_FAILED request_id={} close_exc_type={} "
            "preserved_exc_type={}",
            provider_name,
            request_id,
            type(close_error).__name__,
            active_error_type,
        )


class ProviderAttemptScope:
    """Own one admitted provider attempt and its optional transport resource."""

    def __init__(
        self,
        attempt: ProviderAttempt,
        *,
        provider_name: str,
        request_id: str | None,
    ) -> None:
        self.attempt = attempt
        self._resource: object | None = None
        self._provider_name = provider_name
        self._request_id = request_id
        self._closed = False

    def retain(self, resource: _ResourceT) -> _ResourceT:
        """Retain and return the sole transport resource owned by this scope."""
        if self._closed:
            raise RuntimeError("provider attempt scope is already closed")
        if self._resource is not None:
            raise RuntimeError("provider attempt scope already owns a resource")
        self._resource = resource
        return resource

    async def aclose(self, *, active_error: BaseException | None) -> None:
        """Close transport state without masking the established operation outcome."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._resource is not None:
                await close_provider_stream(
                    self._resource,
                    active_error=active_error,
                    provider_name=self._provider_name,
                    request_id=self._request_id,
                )
        finally:
            await self.attempt.aclose()


class StallGuardedStream:
    """Bound one upstream stream: first chunk by a deadline, then per-chunk idle.

    Counts parsed stream items, not socket bytes, so SSE keep-alive comments
    that defeat the HTTP read timeout cannot hold a request open forever.
    """

    def __init__(
        self,
        source: Any,
        *,
        first_byte_deadline: float,
        first_byte_timeout_s: float,
        idle_timeout_s: float,
    ) -> None:
        self._source = source
        self._iterator: AsyncIterator[Any] | None = None
        self._first_byte_deadline = first_byte_deadline
        self._first_byte_timeout_s = first_byte_timeout_s
        self._idle_timeout_s = idle_timeout_s
        self._started = False

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Any:
        deadline = (
            asyncio.get_running_loop().time() + self._idle_timeout_s
            if self._started
            else self._first_byte_deadline
        )
        timeout = asyncio.timeout_at(deadline)
        try:
            async with timeout:
                if self._iterator is None:
                    self._iterator = aiter(self._source)
                item = await anext(self._iterator)
        except TimeoutError as exc:
            if not timeout.expired():
                raise
            if self._started:
                raise ProviderStreamStalled.idle(self._idle_timeout_s) from exc
            raise ProviderStreamStalled.first_byte(self._first_byte_timeout_s) from exc
        self._started = True
        return item

    async def aclose(self) -> None:
        # The OpenAI SDK stream exposes ``close`` rather than ``aclose``; closing
        # it releases the stalled HTTP connection.
        if callable(getattr(self._source, "aclose", None)):
            await maybe_await_aclose(self._source)
            return
        close = getattr(self._source, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


async def open_guarded_stream(
    open_stream: Callable[[], Awaitable[Any]],
    *,
    first_byte_timeout_s: float,
    idle_timeout_s: float,
) -> StallGuardedStream:
    """Open an upstream stream under the first-byte deadline and guard its chunks.

    The first-byte deadline covers both the response headers and the first
    streamed chunk; afterwards each chunk must arrive within ``idle_timeout_s``.
    """
    deadline = asyncio.get_running_loop().time() + first_byte_timeout_s
    timeout = asyncio.timeout_at(deadline)
    try:
        async with timeout:
            source = await open_stream()
    except TimeoutError as exc:
        if not timeout.expired():
            raise
        raise ProviderStreamStalled.first_byte(first_byte_timeout_s) from exc
    return StallGuardedStream(
        source,
        first_byte_deadline=deadline,
        first_byte_timeout_s=first_byte_timeout_s,
        idle_timeout_s=idle_timeout_s,
    )
