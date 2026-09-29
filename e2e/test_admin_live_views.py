"""Rendered Admin live views: endpoints, usage, key pools, secrets, audit, policy."""

import json
import re

import pytest
from playwright.sync_api import Page, Request, Route, expect

ENDPOINTS = {
    "generated_at": 1000.0,
    "endpoints": [
        {
            "provider_id": "open_router",
            "label": "alpha",
            "circuit": "HEALTHY",
            "configured": True,
            "cooldown_remaining_s": None,
            "cooldown_reason": None,
            "manual_reset_required": False,
            "success_count": 12,
            "failure_count": 1,
            "latency_p50_ms": 210.4,
            "latency_p95_ms": 880.0,
            "last_error": None,
            "recent_errors": [],
        },
        {
            "provider_id": "open_router",
            "label": "beta",
            "circuit": "DEGRADED",
            "configured": True,
            "cooldown_remaining_s": 95.0,
            "cooldown_reason": "rate_limited",
            "manual_reset_required": False,
            "success_count": 3,
            "failure_count": 2,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "last_error": "rate_limited",
            "recent_errors": [{"at": 990.0, "signal": "rate_limited"}],
        },
        {
            "provider_id": "open_router",
            "label": "gamma",
            "circuit": "OPEN",
            "configured": True,
            "cooldown_remaining_s": None,
            "cooldown_reason": None,
            "manual_reset_required": True,
            "success_count": 0,
            "failure_count": 1,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "last_error": "auth_failed",
            "recent_errors": [{"at": 995.0, "signal": "auth_failed"}],
        },
        {
            "provider_id": "groq",
            "label": "primary",
            "circuit": "HALF_OPEN",
            "configured": True,
            "cooldown_remaining_s": None,
            "cooldown_reason": None,
            "manual_reset_required": False,
            "success_count": 0,
            "failure_count": 3,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "last_error": "transport",
            "recent_errors": [],
        },
    ],
}

USAGE = {
    "minutes": 60,
    "generated_at": 1000.0,
    "endpoints": [
        {
            "provider_id": "open_router",
            "label": "alpha",
            "requests": 10,
            "ok": 9,
            "errors": 1,
            "rate_limits": 1,
            "latency_p50_ms": 200.0,
            "latency_p95_ms": 700.0,
            "input_tokens": 1200,
            "output_tokens": 340,
        },
        {
            "provider_id": "open_router",
            "label": "beta",
            "requests": 5,
            "ok": 5,
            "errors": 0,
            "rate_limits": 0,
            "latency_p50_ms": 150.0,
            "latency_p95_ms": 300.0,
            "input_tokens": 400,
            "output_tokens": 90,
        },
    ],
    "sessions": [
        {
            "claude_session_id": "0123456789abcdef",
            "attempts": 15,
            "ok": 14,
            "input_tokens": 1600,
            "output_tokens": 430,
            "first_ts": 900.0,
            "last_ts": 1000.0,
        }
    ],
    "recent": [
        {
            "id": 2,
            "ts": 1000.0,
            "provider_id": "open_router",
            "key_label": "beta",
            "model": "vendor/model-a",
            "input_tokens": 100,
            "output_tokens": 20,
            "latency_ms": 150.0,
            "outcome": "ok",
            "failover_from": "alpha",
            "attempt": 2,
        },
        {
            "id": 1,
            "ts": 999.0,
            "provider_id": "open_router",
            "key_label": "alpha",
            "model": "vendor/model-a",
            "input_tokens": None,
            "output_tokens": None,
            "latency_ms": 90.0,
            "outcome": "rate_limited",
            "failover_from": None,
            "attempt": 1,
        },
    ],
}

USAGE_URL = re.compile(r"/admin/api/usage\?")

PRESETS = {
    "presets": [
        {
            "id": "workspace",
            "permission_mode": "acceptEdits",
            "rules": [
                "DENY  Edit(//**/.git/**)  - never write inside .git; use git",
                "ASK   Bash(git push *)  - git push needs approval",
                "ALLOW Edit(./**)  - edit files in the project",
            ],
        },
        {
            "id": "restricted",
            "permission_mode": "dontAsk",
            "rules": [{"decision": "allow", "rule": "Read", "reason": "read-only"}],
        },
    ]
}


def _json(route: Route, payload: object, status: int = 200) -> None:
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(payload),
    )


def _open(page: Page, base_url: str, width: int = 1280, height: int = 800) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{base_url}/admin")
    expect(page.locator("#messageArea")).to_have_text("")


def _nav(page: Page, name: str) -> None:
    page.locator("#sectionNav").get_by_role("button", name=name, exact=True).click()


def test_endpoint_health_pills_cooldown_and_reset(
    page: Page, admin_base_url: str
) -> None:
    resets: list[str] = []

    def endpoints(route: Route) -> None:
        _json(route, ENDPOINTS)

    def reset(route: Route, request: Request) -> None:
        resets.append(request.url.split("/admin/api/endpoints/")[1])
        _json(route, {"reset": True})

    page.route("**/admin/api/endpoints", endpoints)
    page.route("**/admin/api/endpoints/*/*/reset", reset)
    _open(page, admin_base_url)
    _nav(page, "Endpoints")

    router = page.locator('[data-endpoint-provider="open_router"]')
    expect(router.locator("h3")).to_have_text("OpenRouter")
    expect(router).to_contain_text("1 of 3 keys healthy")
    expect(router.locator('[data-circuit="HEALTHY"]')).to_have_class("status-pill ok")
    expect(router.locator('[data-circuit="DEGRADED"]')).to_have_class(
        "status-pill warn"
    )
    expect(router.locator('[data-circuit="OPEN"]')).to_have_class("status-pill error")
    expect(page.locator('[data-circuit="HALF_OPEN"]')).to_have_class("status-pill info")
    expect(page.locator('[data-circuit="HALF_OPEN"]')).to_have_text("HALF OPEN")
    expect(router.locator("[data-deadline]")).to_have_text(re.compile(r"^1m 3\ds$"))
    expect(router).to_contain_text("210 ms / 880 ms")

    expect(
        page.get_by_role("button", name="Reset open_router alpha", exact=True)
    ).to_be_disabled()
    expect(
        page.get_by_role("button", name="Reset open_router beta", exact=True)
    ).to_be_disabled()
    page.get_by_role("button", name="Reset open_router gamma", exact=True).click()
    expect(page.locator("#messageArea")).to_have_text("Reset open_router gamma")
    assert resets == ["open_router/gamma/reset"]


def test_live_views_degrade_when_backend_routes_are_missing(
    page: Page, admin_base_url: str
) -> None:
    _open(page, admin_base_url)

    _nav(page, "Endpoints")
    expect(page.locator("#endpointsBody")).to_contain_text(
        "Endpoint health is not available on this server yet"
    )
    _nav(page, "Usage")
    expect(page.locator("#usageBody")).to_contain_text("Usage is not available")
    _nav(page, "Secrets")
    expect(page.locator("#vaultStatus")).to_have_text("Unavailable")
    expect(page.locator("#secretSave")).to_be_disabled()
    expect(page.locator("#secretMigrate")).to_be_disabled()
    _nav(page, "Audit")
    expect(page.locator("#auditIntegrity")).to_have_text("Unavailable")
    expect(page.locator("#auditBody")).to_contain_text("not available")
    _nav(page, "Policy")
    expect(page.locator("#policyBody")).to_contain_text(
        "Policy presets is not available"
    )
    expect(page.locator("#messageArea")).to_have_text("")


def test_usage_window_aggregates_and_failover(page: Page, admin_base_url: str) -> None:
    minutes: list[str] = []

    def usage(route: Route, request: Request) -> None:
        minutes.append(request.url.rsplit("minutes=", maxsplit=1)[1])
        _json(route, USAGE)

    page.route(USAGE_URL, usage)
    _open(page, admin_base_url)
    _nav(page, "Usage")

    body = page.locator("#usageBody")
    expect(body.locator(".spark-bar")).to_have_count(2)
    expect(body.locator(".spark-bar").first).to_have_attribute("style", "width: 100%;")
    expect(body.locator(".spark-bar").nth(1)).to_have_attribute("style", "width: 50%;")
    expect(body).to_contain_text("1,200 / 340")
    expect(body.locator(".failover")).to_have_text("\u21aa from alpha")
    expect(body.locator(".status-pill.warn")).to_have_text("rate_limited")
    expect(body).to_contain_text("01234567")

    with page.expect_request(USAGE_URL) as request_info:
        page.locator("#usageWindow").select_option("15")
    assert request_info.value.url.endswith("minutes=15")
    assert minutes[0] == "60"


def test_secrets_save_delete_and_migrate_never_show_values(
    page: Page, admin_base_url: str
) -> None:
    names = ["nvidia"]
    writes: list[tuple[str, str, str | None]] = []

    def secrets(route: Route, request: Request) -> None:
        if request.method == "GET":
            _json(route, {"available": True, "error": None, "names": names})
            return
        name = request.url.rsplit("/", maxsplit=1)[1]
        writes.append((request.method, name, request.post_data))
        if request.method == "PUT":
            names.append(name)
        elif request.method == "DELETE":
            names.remove(name)
        _json(route, {"ok": True})

    def migrate(route: Route) -> None:
        _json(
            route,
            {"migrated": ["NVIDIA_NIM_API_KEY"], "backup": "C:/fcc/.env.bak-1"},
        )

    page.route("**/admin/api/secrets", secrets)
    page.route("**/admin/api/secrets/*", secrets)
    page.route("**/admin/api/secrets/migrate", migrate)  # last route wins
    page.on("dialog", lambda dialog: dialog.accept())
    _open(page, admin_base_url)
    _nav(page, "Secrets")

    expect(page.locator("#vaultStatus")).to_have_text("Available")
    expect(page.locator('[data-secret="nvidia"]')).to_contain_text("vault:nvidia")

    page.locator("#secretName").fill("groq")
    page.locator("#secretValue").fill("sk-super-secret-value")
    page.get_by_role("button", name="Save secret", exact=True).click()
    expect(page.locator('[data-secret="groq"]')).to_be_visible()
    expect(page.locator("#secretValue")).to_have_value("")
    assert writes[0][:2] == ("PUT", "groq")
    assert json.loads(writes[0][2] or "{}") == {"value": "sk-super-secret-value"}
    assert "sk-super-secret-value" not in page.locator("body").inner_text()

    page.get_by_role("button", name="Delete secret nvidia", exact=True).click()
    expect(page.locator('[data-secret="nvidia"]')).to_have_count(0)
    assert writes[1][:2] == ("DELETE", "nvidia")

    page.get_by_role("button", name="Move keys", exact=True).click()
    result = page.locator("#migrateResult")
    expect(result).to_contain_text("NVIDIA_NIM_API_KEY")
    expect(result).to_contain_text("C:/fcc/.env.bak-1")
    expect(result).to_contain_text("Delete it")


def test_secrets_report_vault_error(page: Page, admin_base_url: str) -> None:
    page.route(
        "**/admin/api/secrets",
        lambda route: _json(
            route, {"available": False, "error": "DPAPI unavailable", "names": []}
        ),
    )
    _open(page, admin_base_url)
    _nav(page, "Secrets")
    expect(page.locator("#vaultStatus")).to_have_text("Unavailable")
    expect(page.locator("#vaultError")).to_have_text("DPAPI unavailable")
    expect(page.locator("#secretSave")).to_be_disabled()


def test_audit_integrity_and_filters(page: Page, admin_base_url: str) -> None:
    queries: list[str] = []

    def audit(route: Route, request: Request) -> None:
        query = request.url.split("?", maxsplit=1)[1]
        queries.append(query)
        tampered = "actor=cli" in query
        _json(
            route,
            {
                "verified": not tampered,
                "first_bad_id": 7 if tampered else None,
                "records": [
                    {
                        "id": 1,
                        "ts": "2026-09-29T10:00:00+00:00",
                        "actor": "admin",
                        "action": "secret.put",
                        "resource": "nvidia",
                        "decision": "allow",
                        "outcome": "ok",
                        "payload": {},
                    }
                ],
            },
        )

    page.route(re.compile(r"/admin/api/audit\?"), audit)
    _open(page, admin_base_url)
    _nav(page, "Audit")

    badge = page.locator("#auditIntegrity")
    expect(badge).to_have_text("Chain verified \u2713")
    expect(page.locator("#auditBody")).to_contain_text("secret.put")

    page.locator("#auditAction").fill("secret.put")
    page.locator("#auditActor").fill("cli")
    page.get_by_role("button", name="Filter", exact=True).click()
    expect(badge).to_have_text("Tampered at id 7 \u2717")
    expect(badge).to_have_class("status-pill error")
    assert queries[-1] == "limit=100&action=secret.put&actor=cli"


def test_policy_presets_group_rules(page: Page, admin_base_url: str) -> None:
    page.route("**/admin/api/policy/presets", lambda route: _json(route, PRESETS))
    _open(page, admin_base_url)
    _nav(page, "Policy")

    workspace = page.locator('[data-preset="workspace"]')
    expect(workspace.locator(".status-pill")).to_have_text("acceptEdits")
    expect(workspace.locator(".policy-group.deny summary")).to_have_text("Denies (1)")
    expect(workspace.locator(".policy-group.ask")).to_contain_text("Bash(git push *)")
    expect(workspace.locator(".policy-group.allow")).to_contain_text(
        "edit files in the project"
    )
    restricted = page.locator('[data-preset="restricted"]')
    expect(restricted.locator(".policy-group.allow summary")).to_have_text("Allows (1)")


def test_key_pool_editor_serializes_rows(page: Page, admin_base_url: str) -> None:
    applied: list[dict[str, object]] = []

    def apply(route: Route, request: Request) -> None:
        applied.append(json.loads(request.post_data or "{}")["values"])
        _json(route, {"applied": False, "errors": ["stubbed"]})

    page.route("**/admin/api/config/apply", apply)
    _open(page, admin_base_url)
    section = page.locator("#section-providers")
    section.get_by_role("button", name="Show advanced", exact=True).click()
    field = page.locator('.field[data-key="OPENROUTER_API_KEYS"]')
    expect(field.locator(".key-chip")).to_have_count(0)

    add = field.get_by_role("button", name="Add key", exact=True)
    add.click()
    field.get_by_label("Key 1 label").fill("main")
    field.get_by_label("Key 1 value").fill("sk-one")
    add.click()
    field.get_by_label("Key 2 value").fill("sk-two")
    add.click()
    field.get_by_label("Key 3 label").fill("bad label")
    field.get_by_label("Key 3 value").fill("sk-three")
    expect(field.get_by_label("Key 3 label")).to_have_attribute("aria-invalid", "true")
    expect(field.get_by_label("Key 1 value")).to_have_attribute("type", "password")

    expect(page.locator("#dirtyState")).to_have_text("1 unsaved change")
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page.locator("#messageArea")).to_have_text("stubbed")
    assert applied == [{"OPENROUTER_API_KEYS": "main=sk-one,sk-two"}]

    field.get_by_role("button", name="Remove key 1", exact=True).click()
    field.get_by_role("button", name="Remove key 3", exact=True).click()
    field.get_by_role("button", name="Remove key 2", exact=True).click()
    expect(page.locator("#dirtyState")).to_have_text("No changes")


@pytest.mark.parametrize(
    "admin_base_url",
    [{"OPENROUTER_API_KEYS": "alpha=sk-pool-secret-a,beta=sk-pool-secret-b"}],
    indirect=True,
)
def test_configured_key_pool_is_masked(page: Page, admin_base_url: str) -> None:
    page.route("**/admin/api/endpoints", lambda route: _json(route, ENDPOINTS))
    _open(page, admin_base_url)
    page.locator("#section-providers").get_by_role(
        "button", name="Show advanced", exact=True
    ).click()
    field = page.locator('.field[data-key="OPENROUTER_API_KEYS"]')
    expect(field.locator(".key-chip")).to_have_text(
        [
            "alpha \u2022\u2022\u2022\u2022",
            "beta \u2022\u2022\u2022\u2022",
            "gamma \u2022\u2022\u2022\u2022",
        ]
    )
    expect(field).to_contain_text("replace the whole pool")
    text = page.locator("body").inner_text()
    assert "sk-pool-secret" not in text
    expect(page.locator("#dirtyState")).to_have_text("No changes")


def test_mobile_tables_stack_without_horizontal_scroll(
    page: Page, admin_base_url: str
) -> None:
    page.route("**/admin/api/endpoints", lambda route: _json(route, ENDPOINTS))
    page.route(USAGE_URL, lambda route: _json(route, USAGE))
    _open(page, admin_base_url, width=390, height=844)
    for view in ("Endpoints", "Usage"):
        _nav(page, view)
        expect(page.locator(f"#view-{view.lower()} .data-table").first).to_be_visible()
        expect(page.locator(f"#view-{view.lower()} thead").first).to_be_hidden()
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0
