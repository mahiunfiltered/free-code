"""Admin API: vault secrets, audit log, policy presets and vault-ref config display."""

import json
import os

import pytest
from fastapi.testclient import TestClient

from free_claude_code.config import loader
from free_claude_code.config.paths import managed_env_path
from free_claude_code.core.vault import VaultKeyMissing
from tests.api.support import create_test_app, runtime_for_app

SECRET = "sk-admin-canary-never-return-0123456789"


class MemoryProtector:
    def __init__(self) -> None:
        self.key: bytes | None = None

    def load_key(self, *, create: bool) -> bytes:
        if self.key is None:
            if not create:
                raise VaultKeyMissing("missing")
            self.key = os.urandom(32)
        return self.key


@pytest.fixture(autouse=True)
def memory_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    protector = MemoryProtector()
    monkeypatch.setattr(loader, "platform_protector", lambda _path: protector)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def test_secret_routes_store_list_delete_and_never_return_values(client: TestClient):
    assert client.get("/admin/api/secrets").json() == {
        "available": True,
        "error": None,
        "names": [],
    }
    stored = client.put("/admin/api/secrets/openrouter", json={"value": SECRET})
    assert stored.json() == {"ok": True}
    listed = client.get("/admin/api/secrets")
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json()["names"] == ["openrouter"]
    assert loader.managed_vault().get("openrouter") == SECRET

    assert (
        client.put("/admin/api/secrets/bad name", json={"value": "x"}).status_code
        == 400
    )
    assert client.put("/admin/api/secrets/x", json={"value": ""}).status_code == 422
    assert client.delete("/admin/api/secrets/openrouter").json() == {"ok": True}
    assert client.delete("/admin/api/secrets/openrouter").status_code == 404

    audit = client.get("/admin/api/audit").json()
    assert audit["verified"] is True and audit["first_bad_id"] is None
    assert [r["action"] for r in audit["records"]] == [
        "secret.delete",
        "secret.delete",
        "secret.set",
    ]
    for response in (listed, client.get("/admin/api/audit")):
        assert SECRET not in response.text


def test_migrate_route_moves_env_keys_into_the_vault(client: TestClient):
    path = managed_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"OPENROUTER_API_KEY={SECRET}\n", "utf-8")

    body = client.post("/admin/api/secrets/migrate").json()

    assert body["migrated"] == ["OPENROUTER_API_KEY"] and body["backup"]
    assert "vault:OPENROUTER_API_KEY" in path.read_text("utf-8")


def test_config_shows_vault_reference_not_the_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    loader.managed_vault().set("openrouter", SECRET)
    path = managed_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("OPENROUTER_API_KEY=vault:openrouter\n", "utf-8")
    loader.clear_settings_cache()

    response = client.get("/admin/api/config")

    assert response.status_code == 200 and SECRET not in response.text
    field = next(
        f for f in response.json()["fields"] if f["key"] == "OPENROUTER_API_KEY"
    )
    assert field["value"] == "vault:openrouter" and field["configured"] is True


def test_audit_route_filters_and_detects_tampering():
    app = create_test_app()
    client = TestClient(app, client=("127.0.0.1", 50000))
    runtime = runtime_for_app(app)
    audit = runtime.workbench.audit
    audit.append(actor="admin", action="config.apply", outcome="applied")
    audit.append(actor="user", action="permission.decision", decision="allow")

    only_user = client.get("/admin/api/audit", params={"actor": "user"}).json()
    assert [r["action"] for r in only_user["records"]] == ["permission.decision"]
    limited = client.get("/admin/api/audit", params={"limit": 1}).json()
    assert len(limited["records"]) == 1
    assert client.get("/admin/api/audit", params={"limit": 0}).status_code == 422

    store = audit._store  # tamper like an attacker with raw DB access would
    with store.transaction() as conn:
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute("UPDATE audit_log SET decision = 'deny' WHERE id = 2")
    tampered = client.get("/admin/api/audit").json()
    assert tampered["verified"] is False and tampered["first_bad_id"] == 2


def test_config_apply_is_audited_with_keys_only(client: TestClient):
    client.post(
        "/admin/api/config/apply", json={"values": {"OPENROUTER_API_KEY": SECRET}}
    )
    records = client.get("/admin/api/audit", params={"action": "config.apply"}).json()
    [record] = records["records"]
    assert record["payload"] == {"keys": ["OPENROUTER_API_KEY"]}
    assert SECRET not in json.dumps(records)


def test_admin_policy_presets_and_loopback_guard():
    local = TestClient(create_test_app(), client=("127.0.0.1", 50000))
    presets = local.get("/admin/api/policy/presets").json()["presets"]
    assert [p["id"] for p in presets] == ["restricted", "workspace", "privileged"]
    remote = TestClient(create_test_app(), client=("203.0.113.9", 50000))
    for method, path in (
        ("get", "/admin/api/secrets"),
        ("put", "/admin/api/secrets/x"),
        ("delete", "/admin/api/secrets/x"),
        ("post", "/admin/api/secrets/migrate"),
        ("get", "/admin/api/audit"),
        ("get", "/admin/api/policy/presets"),
    ):
        kwargs = {"json": {"value": "v"}} if method == "put" else {}
        assert getattr(remote, method)(path, **kwargs).status_code == 403, path
