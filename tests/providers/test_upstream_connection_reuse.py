"""Upstream SSE connections return to the pool after ``[DONE]`` (no TLS per request)."""

import asyncio

import httpcore2
import httpx2
import pytest

from free_claude_code.config.nim import NimSettings
from free_claude_code.core.anthropic.models import MessagesRequest
from free_claude_code.providers.nvidia_nim import NvidiaNimProvider
from free_claude_code.providers.openai_chat.provider import (
    UPSTREAM_KEEPALIVE_EXPIRY_S,
)
from tests.providers.support import immediate_admission, make_provider_config

_SSE_BODY = (
    b'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"m",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},'
    b'"finish_reason":null}]}\n\n'
    b'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"m",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


async def _serve_sse(connections: list[int]):
    """Minimal HTTP/1.1 keep-alive server sending chunked SSE, counting sockets."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        connections.append(1)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value)
                await reader.readexactly(length)
                writer.write(
                    b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                    b"transfer-encoding: chunked\r\n\r\n"
                )
                writer.write(b"%x\r\n%s\r\n" % (len(_SSE_BODY), _SSE_BODY))
                await writer.drain()
                # The terminator trails [DONE], as with real upstreams.
                await asyncio.sleep(0.01)
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


@pytest.mark.asyncio
async def test_sequential_streams_reuse_one_upstream_connection():
    connections: list[int] = []
    server = await _serve_sse(connections)
    port = server.sockets[0].getsockname()[1]
    provider = NvidiaNimProvider(
        make_provider_config(api_key="k", base_url=f"http://127.0.0.1:{port}/v1"),
        nim_settings=NimSettings(),
        admission=immediate_admission(),
    )
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    try:
        for _ in range(3):
            text = "".join([event async for event in provider.stream_messages(request)])
            assert '"ok"' in text
            assert "message_stop" in text
    finally:
        await provider.cleanup()
        server.close()
        await server.wait_closed()

    assert len(connections) == 1


@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:9"])
def test_upstream_client_keeps_idle_connections_warm(proxy):
    """Idle upstream connections outlive httpx's 5 s default (no TLS per request)."""
    config = make_provider_config(
        api_key="test_key", base_url="https://test.api.nvidia.com/v1", proxy=proxy
    )
    provider = NvidiaNimProvider(
        config, nim_settings=NimSettings(), admission=immediate_admission()
    )
    client = provider._client._client
    transports = [client._transport, *client._mounts.values()]
    if proxy is not None:
        transports = transports[1:]  # every request goes through the proxy mount
    assert transports
    for transport in transports:
        assert isinstance(transport, httpx2.AsyncHTTPTransport)
        assert isinstance(transport._pool, httpcore2.AsyncConnectionPool)
        assert transport._pool._keepalive_expiry == UPSTREAM_KEEPALIVE_EXPIRY_S > 5
