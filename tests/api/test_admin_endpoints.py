"""Admin JSON API for provider key pool health and usage."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from free_claude_code.api.app import create_app
from free_claude_code.api.ports import ApiServices
from free_claude_code.config import paths
from free_claude_code.config.provider_keys import ProviderKey
from free_claude_code.config.settings import Settings
from free_claude_code.core.storage import Store
from free_claude_code.providers.endpoint_health import HealthSignal
from free_claude_code.providers.key_pool import EndpointPool
from free_claude_code.providers.usage_records import UsageRecord
from free_claude_code.runtime.application import ApplicationRuntime
from free_claude_code.runtime.bootstrap import build_asgi_app
from free_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.api.support import create_test_app

CANARY = "nvapi-CANARY-admin-never-serialize"


def _app(pool: EndpointPool | None) -> TestClient:
    manager = ProviderRuntimeManager(Settings(), endpoint_pool=pool)
    runtime = ApplicationRuntime(manager, transcriber=None)
    app = create_app(
        ApiServices(requests=manager, admin=runtime, tasks=runtime, endpoints=manager)
    )
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def pool(tmp_path) -> Iterator[EndpointPool]:
    store = Store(tmp_path / "fcc.db")
    pool = EndpointPool(store)
    bad = pool.register("nvidia_nim", ProviderKey("bad", f"{CANARY}-bad"))
    good = pool.register("nvidia_nim", ProviderKey("good", f"{CANARY}-good"))
    pool.health.record_failure(bad, HealthSignal.AUTH_FAILED)
    pool.health.record_success(good, 812.0)
    for label, outcome, failover_from, attempt in (
        ("bad", "auth_failed", None, 1),
        ("good", "ok", "bad", 2),
    ):
        pool.record_usage(
            UsageRecord(
                provider_id="nvidia_nim",
                key_label=label,
                model="z-ai/glm4.7",
                request_id="req_1",
                claude_session_id="sess-1",
                input_tokens=None if outcome != "ok" else 20,
                output_tokens=None if outcome != "ok" else 5,
                latency_ms=812.0 if outcome == "ok" else 90.0,
                outcome=outcome,
                attempt=attempt,
                failure_kind="authentication" if outcome != "ok" else None,
                failover_from=failover_from,
            )
        )
    yield pool
    store.close()


def test_endpoint_health_lists_each_key(pool: EndpointPool) -> None:
    response = _app(pool).get("/admin/api/endpoints")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["enabled"] is True
    by_label = {item["label"]: item for item in body["endpoints"]}
    assert by_label["bad"]["circuit"] == "OPEN"
    assert by_label["bad"]["manual_reset_required"] is True
    assert by_label["bad"]["recent_errors"][0]["signal"] == "auth_failed"
    assert by_label["good"]["circuit"] == "HEALTHY"
    assert by_label["good"]["latency_p50_ms"] == 812.0
    assert by_label["good"]["configured"] is True
    assert CANARY not in response.text


def test_reset_endpoint_closes_the_circuit(pool: EndpointPool) -> None:
    client = _app(pool)

    response = client.post("/admin/api/endpoints/nvidia_nim/bad/reset")

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": "nvidia_nim",
        "label": "bad",
        "reset": True,
    }
    by_label = {
        item["label"]: item
        for item in client.get("/admin/api/endpoints").json()["endpoints"]
    }
    assert by_label["bad"]["circuit"] == "HEALTHY"
    assert client.post("/admin/api/endpoints/nvidia_nim/nope/reset").status_code == 404


def test_usage_summary_aggregates_and_sessions(pool: EndpointPool) -> None:
    response = _app(pool).get("/admin/api/usage", params={"minutes": 30})

    assert response.status_code == 200
    body = response.json()
    assert body["minutes"] == 30
    by_label = {item["label"]: item for item in body["endpoints"]}
    assert by_label["bad"]["errors"] == 1 and by_label["bad"]["requests"] == 1
    assert by_label["good"]["latency_p95_ms"] == 812.0
    assert by_label["good"]["output_tokens"] == 5
    assert body["sessions"][0]["claude_session_id"] == "sess-1"
    assert body["sessions"][0]["attempts"] == 2
    assert [row["failover_from"] for row in body["recent"]] == ["bad", None]
    assert CANARY not in response.text


def test_usage_rejects_invalid_window(pool: EndpointPool) -> None:
    client = _app(pool)
    assert client.get("/admin/api/usage", params={"minutes": 0}).status_code == 422
    assert client.get("/admin/api/usage").json()["minutes"] == 60


def test_endpoint_routes_are_loopback_only(pool: EndpointPool) -> None:
    local = _app(pool)
    remote = TestClient(local.app, client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/endpoints").status_code == 403
    assert remote.get("/admin/api/usage").status_code == 403
    assert remote.post("/admin/api/endpoints/nvidia_nim/bad/reset").status_code == 403


def test_routes_report_unavailable_without_a_pool_port() -> None:
    client = TestClient(create_test_app(), client=("127.0.0.1", 50000))
    assert client.get("/admin/api/endpoints").status_code == 503


def test_manager_without_pool_reports_disabled() -> None:
    client = _app(None)
    assert client.get("/admin/api/endpoints").json() == {
        "enabled": False,
        "endpoints": [],
    }
    assert client.get("/admin/api/usage").json() == {"enabled": False, "minutes": 60.0}
    assert client.post("/admin/api/endpoints/nvidia_nim/x/reset").status_code == 404


def test_bootstrap_creates_the_store_and_wires_the_endpoint_port() -> None:
    settings = Settings.model_validate(
        {"MODEL": "nvidia_nim/m", "NVIDIA_NIM_API_KEYS": f"a={CANARY}"}
    )
    with patch("free_claude_code.runtime.bootstrap.configure_logging"):
        asgi_app = build_asgi_app(settings)

    assert (paths.config_dir_path() / "fcc.db").is_file()
    app = asgi_app.app
    assert isinstance(app, FastAPI)
    services = app.state.services
    assert services.endpoints is asgi_app.runtime.provider_manager
    assert services.endpoints.endpoint_health()["enabled"] is True
