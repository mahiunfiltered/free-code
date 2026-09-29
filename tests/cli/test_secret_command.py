import io
import os
from pathlib import Path

import pytest

from free_claude_code.cli import secret_command
from free_claude_code.config.env_files import dotenv_values_from_file
from free_claude_code.config.paths import managed_env_path
from free_claude_code.core.vault import Vault, VaultKeyMissing

SECRET = "nvapi-cli-secret-0123456789"


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
    monkeypatch.setattr(secret_command, "managed_vault", lambda: instance)
    return instance


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(secret_command.sys, "stdin", io.StringIO(text))


def test_set_reads_value_from_stdin(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, SECRET + "\n")
    assert secret_command.main(["set", "NIM"]) == 0
    assert vault.get("NIM") == SECRET
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert "NIM=vault:NIM" in out.out


def test_set_uses_hidden_prompt_on_tty(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    tty = io.StringIO("")
    monkeypatch.setattr(tty, "isatty", lambda: True)
    monkeypatch.setattr(secret_command.sys, "stdin", tty)
    monkeypatch.setattr(secret_command.getpass, "getpass", lambda _prompt: SECRET)
    assert secret_command.main(["set", "NIM"]) == 0
    assert vault.get("NIM") == SECRET


def test_set_refuses_value_on_argv(
    vault: Vault, capsys: pytest.CaptureFixture[str]
) -> None:
    assert secret_command.main(["set", "NIM", SECRET]) == 2
    assert vault.names() == []
    assert "Refusing" in capsys.readouterr().err


def test_set_rejects_empty_value(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _stdin(monkeypatch, "\n")
    assert secret_command.main(["set", "NIM"]) == 2
    assert vault.names() == []


def test_set_rejects_bad_name(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, SECRET)
    assert secret_command.main(["set", "bad/name"]) == 1
    assert "Invalid secret name" in capsys.readouterr().err


def test_list_and_delete(vault: Vault, capsys: pytest.CaptureFixture[str]) -> None:
    vault.set("B", "2")
    vault.set("A", "1")
    assert secret_command.main(["list"]) == 0
    assert capsys.readouterr().out.split() == ["A", "B"]
    assert secret_command.main(["delete", "A"]) == 0
    assert secret_command.main(["delete", "A"]) == 1
    assert secret_command.list_secrets() == ["B"]


def test_admin_functions_never_return_values(vault: Vault) -> None:
    secret_command.set_secret("K", SECRET)
    assert secret_command.list_secrets() == ["K"]
    assert secret_command.delete_secret("K") is True
    assert secret_command.list_secrets() == []


def test_migrate_moves_plaintext_keys_with_backup(
    vault: Vault, capsys: pytest.CaptureFixture[str]
) -> None:
    env = managed_env_path()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(
        f"NVIDIA_NIM_API_KEY={SECRET}\n"
        "OPENROUTER_API_KEYS=k1,k2\n"
        "OPENAI_API_KEY=vault:existing\n"
        "MODEL=nvidia_nim/test-model\n",
        encoding="utf-8",
    )
    original = env.read_text(encoding="utf-8")

    assert secret_command.main(["migrate"]) == 0

    values = dotenv_values_from_file(env)
    assert values["NVIDIA_NIM_API_KEY"] == "vault:NVIDIA_NIM_API_KEY"
    assert values["OPENROUTER_API_KEYS"] == "vault:OPENROUTER_API_KEYS"
    assert values["OPENAI_API_KEY"] == "vault:existing"
    assert values["MODEL"] == "nvidia_nim/test-model"
    assert SECRET not in env.read_text(encoding="utf-8")
    assert vault.get("NVIDIA_NIM_API_KEY") == SECRET
    assert vault.get("OPENROUTER_API_KEYS") == "k1,k2"
    backups = list(env.parent.glob(".env.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert SECRET not in capsys.readouterr().out

    assert secret_command.main(["migrate"]) == 0
    assert "No plaintext" in capsys.readouterr().out


def test_migrate_without_env_file(vault: Vault) -> None:
    assert secret_command.migrate_env() == ([], None)
