"""Rendered NVIDIA model slots on the Providers page."""

from playwright.sync_api import Page, expect


def test_fill_row_and_save_models(page: Page, admin_base_url: str) -> None:
    page.goto(f"{admin_base_url}/admin")
    rows = page.locator("#nvidiaRows .nvidia-row")
    expect(rows).to_have_count(8)

    rows.nth(0).locator(".nvidia-model").fill("nvidia_nim/vendor/first")
    rows.nth(1).locator(".nvidia-model").fill("vendor/second")
    rows.nth(1).locator(".nvidia-default").check()

    with page.expect_request(
        lambda r: r.url.endswith("/admin/api/nvidia-models") and r.method == "POST"
    ) as posted:
        page.get_by_role("button", name="Save models").click()
    body = posted.value.post_data_json
    assert isinstance(body, dict)
    slots = body["slots"]
    assert [(s["model"], s["default"]) for s in slots[:3]] == [
        ("nvidia_nim/vendor/first", False),
        ("vendor/second", True),
        ("", False),
    ]

    expect(page.locator("#messageArea")).to_have_text("NVIDIA models saved.")
    expect(rows.nth(0).locator(".nvidia-model")).to_have_value("vendor/second")
    expect(rows.nth(0).locator(".nvidia-default")).to_be_checked()
    expect(rows.nth(1).locator(".nvidia-model")).to_have_value("vendor/first")
