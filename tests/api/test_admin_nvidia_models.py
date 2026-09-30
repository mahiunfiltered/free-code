from fastapi.testclient import TestClient

from free_claude_code.config import loader
from free_claude_code.config.admin import nvidia_slots
from free_claude_code.core.vault import SecretNotFound
from tests.api.support import create_test_app, provider_manager_for_app
from tests.api.test_admin import _local_client, _set_home
from tests.api.test_admin_endpoints import CANARY

_KEYS = (
    "MODEL",
    "MODEL_FALLBACKS",
    "MODEL_FABLE",
    "MODEL_OPUS",
    "MODEL_SONNET",
    "MODEL_HAIKU",
    "CHAT_MODELS",
    "NVIDIA_NIM_API_KEY",
    "NVIDIA_NIM_API_KEYS",
    "FCC_ENV_FILE",
)


class _FakeVault:
    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self.secrets[name] = value

    def get(self, name: str) -> str:
        if name not in self.secrets:
            raise SecretNotFound(name)
        return self.secrets[name]


def _setup(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    vault = _FakeVault()
    monkeypatch.setattr(nvidia_slots, "managed_vault", lambda: vault)
    monkeypatch.setattr(loader, "managed_vault", lambda: vault)
    return _local_client(create_test_app()), vault


def test_save_applies_hot_and_never_leaks_keys(monkeypatch, tmp_path):
    client, vault = _setup(monkeypatch, tmp_path)
    assert client.get("/admin/api/nvidia-models").json()["max_slots"] == 8

    response = client.post(
        "/admin/api/nvidia-models",
        json={
            "slots": [
                {"model": "a/one", "key": f"{CANARY}-1", "default": False},
                {"model": "nvidia_nim/b/two", "key": f"{CANARY}-2", "default": True},
                {"model": "", "key": None, "default": False},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert CANARY not in response.text
    env = (tmp_path / ".fcc" / ".env").read_text(encoding="utf-8")
    assert "NVIDIA_NIM_API_KEYS=vault:nvidia_pool" in env
    assert CANARY not in env
    assert set(vault.secrets["nvidia_pool"].split(",")) == {
        f"one={CANARY}-1",
        f"two={CANARY}-2",
    }
    assert "CHAT_MODELS=nvidia_nim/b/two,nvidia_nim/a/one" in env
    assert provider_manager_for_app(client.app).current_settings().model_fallbacks == (
        "nvidia_nim/a/one",
    )


def test_invalid_model_is_422_and_remote_is_403(monkeypatch, tmp_path):
    client, _ = _setup(monkeypatch, tmp_path)
    bad = client.post("/admin/api/nvidia-models", json={"slots": [{"model": "a b"}]})
    assert bad.status_code == 422

    remote = TestClient(client.app, client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/nvidia-models").status_code == 403
    posted = remote.post("/admin/api/nvidia-models", json={"slots": []})
    assert posted.status_code == 403
