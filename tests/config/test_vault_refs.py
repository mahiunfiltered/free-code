import os
from pathlib import Path

import pytest

from free_claude_code.config import loader
from free_claude_code.config.loader import (
    VaultReferenceError,
    compose_settings_snapshot,
    resolve_vault_refs,
)
from free_claude_code.core.vault import Vault, VaultKeyMissing, VaultUnavailable

SECRET = "nvapi-resolved-secret-abcdef"


class MemoryProtector:
    def __init__(self) -> None:
        self.key: bytes | None = None

    def load_key(self, *, create: bool) -> bytes:
        if self.key is None:
            if not create:
                raise VaultKeyMissing("missing")
            self.key = os.urandom(32)
        return self.key


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    instance = Vault(tmp_path / "vault.bin", MemoryProtector())
    monkeypatch.setattr(loader, "managed_vault", lambda: instance)
    return instance


def test_vault_reference_resolves_into_settings(vault: Vault) -> None:
    vault.set("nim", SECRET)
    snapshot = compose_settings_snapshot({"NVIDIA_NIM_API_KEY": " vault:nim "}, {})
    assert snapshot.settings.nvidia_nim_api_key == SECRET


def test_process_env_reference_also_resolves(vault: Vault) -> None:
    vault.set("router", SECRET)
    snapshot = compose_settings_snapshot({}, {"OPENROUTER_API_KEY": "vault:router"})
    assert snapshot.settings.open_router_api_key == SECRET


def test_missing_secret_names_it_without_leaking(vault: Vault) -> None:
    vault.set("present", SECRET)
    with pytest.raises(VaultReferenceError) as exc:
        compose_settings_snapshot({"NVIDIA_NIM_API_KEY": "vault:absent"}, {})
    message = str(exc.value)
    assert "NVIDIA_NIM_API_KEY" in message
    assert "'absent'" in message
    assert "fcc-secret set absent" in message
    assert SECRET not in message


def test_plain_values_never_open_the_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> Vault:
        raise AssertionError("vault must not be opened")

    monkeypatch.setattr(loader, "managed_vault", fail)
    values = {"NVIDIA_NIM_API_KEY": "plain", "OTHER": "vaultish"}
    assert resolve_vault_refs(values) == values
    snapshot = compose_settings_snapshot({"NVIDIA_NIM_API_KEY": "plain"}, {})
    assert snapshot.settings.nvidia_nim_api_key == "plain"


def test_unavailable_vault_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> Vault:
        raise VaultUnavailable("no keyring")

    monkeypatch.setattr(loader, "managed_vault", unavailable)
    with pytest.raises(VaultReferenceError, match=r"NVIDIA_NIM_API_KEY.*no keyring"):
        resolve_vault_refs({"NVIDIA_NIM_API_KEY": "vault:nim"})


def test_invalid_reference_name_is_reported(vault: Vault) -> None:
    with pytest.raises(VaultReferenceError, match="Invalid secret name"):
        resolve_vault_refs({"NVIDIA_NIM_API_KEY": "vault:bad name"})


def test_managed_vault_lives_in_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loader, "platform_protector", lambda _path: MemoryProtector())
    assert loader.managed_vault().path == loader.paths.config_dir_path() / "vault.bin"
