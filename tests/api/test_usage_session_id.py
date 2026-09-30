"""Claude Code's session header reaches provider usage rows and per-session totals."""

import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from free_claude_code.api.app import create_app
from free_claude_code.api.ports import ApiServices
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings
from free_claude_code.core.request_context import (
    bind_claude_session_id,
    current_claude_session_id,
)
from free_claude_code.core.storage import Store
from free_claude_code.core.trace import extract_claude_session_id_from_headers
from free_claude_code.providers.key_pool import EndpointPool
from free_claude_code.providers.runtime import ProviderRuntime
from free_claude_code.providers.runtime.factory import create_provider
from free_claude_code.runtime.application import ApplicationRuntime
from free_claude_code.runtime.provider_manager import ProviderRuntimeManager

SESSION = "0b6f7c1e-5a4d-4c3b-9f2e-1d0c9b8a7f6e"


class _Upstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["content-length"]))
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "hi"}}]},
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        ]
        body = "".join(
            "data: "
            + json.dumps(
                {"id": "c", "object": "chat.completion.chunk", "created": 1}
                | {"model": "m"}
                | chunk
            )
            + "\n\n"
            for chunk in chunks
        )
        payload = (body + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def upstream_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def test_claude_code_session_header_is_recognised() -> None:
    assert (
        extract_claude_session_id_from_headers({"X-Claude-Code-Session-Id": SESSION})
        == SESSION
    )
    assert extract_claude_session_id_from_headers({"x-other": "1"}) is None


def test_bind_is_scoped_and_resets() -> None:
    assert current_claude_session_id() is None
    with bind_claude_session_id("s-1"):
        assert current_claude_session_id() == "s-1"
    assert current_claude_session_id() is None


def test_messages_request_records_session_on_usage_rows(
    tmp_path, upstream_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        PROVIDER_CATALOG,
        "nvidia_nim",
        replace(PROVIDER_CATALOG["nvidia_nim"], default_base_url=upstream_url),
    )
    store = Store(tmp_path / "fcc.db")
    pool = EndpointPool(store)
    settings = Settings.model_validate(
        {"MODEL": "nvidia_nim/m", "NVIDIA_NIM_API_KEYS": "a=k-a,b=k-b"}
    )
    manager = ProviderRuntimeManager(
        settings,
        runtime_factory=partial(
            ProviderRuntime,
            provider_constructor=partial(create_provider, endpoint_pool=pool),
        ),
        endpoint_pool=pool,
    )
    runtime = ApplicationRuntime(manager, transcriber=None)
    app = create_app(
        ApiServices(requests=manager, admin=runtime, tasks=runtime, endpoints=manager)
    )
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            for session in (SESSION, SESSION, None):
                headers = {"x-claude-code-session-id": session} if session else {}
                response = client.post(
                    "/v1/messages",
                    headers=headers,
                    json={
                        "model": "claude-sonnet-4-5",
                        "max_tokens": 32,
                        "stream": True,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                assert response.status_code == 200
                assert "message_stop" in response.text

            rows = list(reversed(pool.usage.recent(10)))
            assert [row["claude_session_id"] for row in rows] == [
                SESSION,
                SESSION,
                None,
            ]
            assert all(row["outcome"] == "ok" for row in rows)

            usage = client.get("/admin/api/usage").json()
            [session_totals] = usage["sessions"]
            assert session_totals["claude_session_id"] == SESSION
            assert session_totals["attempts"] == 2
            assert session_totals["output_tokens"] == 6
    finally:
        store.close()
