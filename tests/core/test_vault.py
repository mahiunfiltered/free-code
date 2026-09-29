import logging
import os
import sys
from pathlib import Path

import pytest

from free_claude_code.core import vault as vault_module
from free_claude_code.core.vault import (
    MAGIC,
    DpapiProtector,
    KeychainProtector,
    SecretNotFound,
    SecretToolProtector,
    Vault,
    VaultCorrupted,
    VaultKeyMissing,
    VaultUnavailable,
    platform_protector,
)

SECRET = "nvapi-super-secret-value-123456"


class MemoryProtector:
    def __init__(self, key: bytes | None = None) -> None:
        self.key = key
        self.calls = 0

    def load_key(self, *, create: bool) -> bytes:
        self.calls += 1
        if self.key is None:
            if not create:
                raise VaultKeyMissing("memory key missing")
            self.key = os.urandom(32)
        return self.key


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    return tmp_path / "vault.bin"


def test_roundtrip_set_get_names_delete(vault_path: Path) -> None:
    vault = Vault(vault_path, MemoryProtector())
    vault.set("NIM_KEY", SECRET)
    vault.set("other", "x")
    vault.set("other", "y")
    assert vault.get("NIM_KEY") == SECRET
    assert vault.get("other") == "y"
    assert vault.names() == ["NIM_KEY", "other"]
    assert vault.delete("other") is True
    assert vault.delete("other") is False
    assert vault.names() == ["NIM_KEY"]


def test_file_is_encrypted_and_hides_names(vault_path: Path) -> None:
    Vault(vault_path, MemoryProtector()).set("NIM_KEY", SECRET)
    raw = vault_path.read_bytes()
    assert raw.startswith(MAGIC)
    assert SECRET.encode() not in raw
    assert b"NIM_KEY" not in raw


def test_second_instance_sees_other_writers(vault_path: Path) -> None:
    protector = MemoryProtector()
    first, second = Vault(vault_path, protector), Vault(vault_path, protector)
    first.set("a", "1")
    second.set("b", "2")
    assert first.names() == ["a", "b"]


def test_empty_vault_does_not_touch_protector(vault_path: Path) -> None:
    protector = MemoryProtector()
    vault = Vault(vault_path, protector)
    assert vault.names() == []
    with pytest.raises(SecretNotFound, match="fcc-secret set missing"):
        vault.get("missing")
    assert protector.calls == 0


def test_wrong_key_is_a_clear_integrity_error(vault_path: Path) -> None:
    Vault(vault_path, MemoryProtector()).set("k", SECRET)
    with pytest.raises(VaultCorrupted, match="integrity") as exc:
        Vault(vault_path, MemoryProtector(os.urandom(32))).get("k")
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("damage", ["flip", "truncate", "garbage"])
def test_corrupted_file_is_rejected(vault_path: Path, damage: str) -> None:
    protector = MemoryProtector()
    Vault(vault_path, protector).set("k", SECRET)
    raw = bytearray(vault_path.read_bytes())
    if damage == "flip":
        raw[-1] ^= 0x01
    elif damage == "truncate":
        raw = raw[:10]
    else:
        raw = bytearray(b"not a vault at all, just plain text")
    vault_path.write_bytes(bytes(raw))
    with pytest.raises(VaultCorrupted):
        Vault(vault_path, protector).names()


def test_existing_vault_never_mints_a_new_key(vault_path: Path) -> None:
    Vault(vault_path, MemoryProtector()).set("k", SECRET)
    with pytest.raises(VaultKeyMissing):
        Vault(vault_path, MemoryProtector()).set("k2", "v")


def test_protector_key_size_is_checked(vault_path: Path) -> None:
    with pytest.raises(VaultCorrupted, match="256 bits"):
        Vault(vault_path, MemoryProtector(b"short")).set("k", "v")


@pytest.mark.parametrize("name", ["", "has space", "a/b", "x" * 129, "vault:x"])
def test_invalid_names_rejected(vault_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="Invalid secret name"):
        Vault(vault_path, MemoryProtector()).set(name, "v")


def test_empty_value_rejected(vault_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        Vault(vault_path, MemoryProtector()).set("k", "")


def test_atomic_write_keeps_old_file_on_failure(
    vault_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protector = MemoryProtector()
    vault = Vault(vault_path, protector)
    vault.set("k", "old")
    before = vault_path.read_bytes()

    def boom(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(vault_module.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        vault.set("k", "new")
    monkeypatch.undo()
    assert vault_path.read_bytes() == before
    assert Vault(vault_path, protector).get("k") == "old"
    assert sorted(p.name for p in vault_path.parent.iterdir()) == [
        "vault.bin",
        "vault.bin.lock",
    ]


def test_values_absent_from_repr_and_logs(
    vault_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    vault = Vault(vault_path, MemoryProtector())
    vault.set("k", SECRET)
    vault.get("k")
    assert SECRET not in repr(vault)
    assert SECRET not in caplog.text


def test_fake_subprocess_backends_keep_key_off_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bytes | None]] = []
    store: dict[str, bytes] = {}

    class Done:
        def __init__(self, code: int, out: bytes = b"") -> None:
            self.returncode, self.stdout, self.stderr = code, out, b""

    def fake_run(argv: list[str], stdin: bytes | None = None) -> Done:
        calls.append((argv, stdin))
        if argv[:2] in (
            ["security", "find-generic-password"],
            ["secret-tool", "lookup"],
        ):
            if argv[0] in store:
                return Done(0, store[argv[0]])
            return Done(44 if argv[0] == "security" else 1)
        assert stdin is not None
        hex_key = (
            stdin.decode().split()[-1] if argv[0] == "security" else stdin.decode()
        )
        store[argv[0]] = hex_key.encode()
        return Done(0)

    monkeypatch.setattr(vault_module, "_run", fake_run)
    for protector in (KeychainProtector(), SecretToolProtector()):
        with pytest.raises(VaultKeyMissing):
            protector.load_key(create=False)
        key = protector.load_key(create=True)
        assert protector.load_key(create=False) == key
        for argv, _stdin in calls:
            assert key.hex() not in " ".join(argv)


def test_linux_without_secret_tool_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(vault_module.sys, "platform", "linux")
    monkeypatch.setattr(vault_module.shutil, "which", lambda _name: None)
    with pytest.raises(VaultUnavailable, match="libsecret"):
        platform_protector(tmp_path / "vault.key")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_real_dpapi_roundtrip(tmp_path: Path) -> None:
    key_path = tmp_path / "vault.key"
    protector = DpapiProtector(key_path)
    with pytest.raises(VaultKeyMissing):
        protector.load_key(create=False)
    key = protector.load_key(create=True)
    assert len(key) == 32
    assert key not in key_path.read_bytes()
    assert DpapiProtector(key_path).load_key(create=False) == key

    vault_path = tmp_path / "vault.bin"
    Vault(vault_path, platform_protector(key_path)).set("k", SECRET)
    assert Vault(vault_path, DpapiProtector(key_path)).get("k") == SECRET


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_garbage_blob_is_key_missing(tmp_path: Path) -> None:
    key_path = tmp_path / "vault.key"
    key_path.write_bytes(b"not a dpapi blob")
    with pytest.raises(VaultKeyMissing, match="DPAPI"):
        DpapiProtector(key_path).load_key(create=False)
