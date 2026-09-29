"""Registered secret values (vault-resolved or admin-set) never reach diagnostics or logs."""

import json
import os

import pytest
from loguru import logger

from free_claude_code.config import loader
from free_claude_code.config.logging_config import configure_logging
from free_claude_code.core.diagnostics import (
    redact_registered_secrets,
    redact_sensitive_error_text,
    register_secret_value,
)
from free_claude_code.core.vault import VaultKeyMissing


class MemoryProtector:
    def __init__(self) -> None:
        self.key: bytes | None = None

    def load_key(self, *, create: bool) -> bytes:
        if self.key is None:
            if not create:
                raise VaultKeyMissing("missing")
            self.key = os.urandom(32)
        return self.key


def test_registered_values_are_redacted_and_short_values_ignored():
    register_secret_value("  plain-registry-secret-4411  ")
    register_secret_value("short")
    text = "value=plain-registry-secret-4411 and short"
    assert redact_registered_secrets(text) == "value=<redacted> and short"
    assert "plain-registry" not in redact_sensitive_error_text(text)


def test_vault_resolution_registers_secrets(monkeypatch: pytest.MonkeyPatch):
    protector = MemoryProtector()
    monkeypatch.setattr(loader, "platform_protector", lambda _path: protector)
    loader.managed_vault().set("custom", "vault-held-value-88213")

    resolved = loader.resolve_vault_refs({"X_API_KEY": "vault:custom", "Y": "1"})

    assert resolved == {"X_API_KEY": "vault-held-value-88213", "Y": "1"}
    assert redact_registered_secrets("got vault-held-value-88213") == "got <redacted>"


def test_log_file_lines_redact_registered_secrets(tmp_path):
    register_secret_value("log-line-secret-value-5521")
    log_path = tmp_path / "server.log"
    configure_logging(log_path, force=True)
    logger.bind(request_id="log-line-secret-value-5521").info(
        "leaked log-line-secret-value-5521"
    )
    logger.complete()

    lines = log_path.read_text("utf-8").strip().splitlines()
    assert "log-line-secret-value-5521" not in log_path.read_text("utf-8")
    assert json.loads(lines[-1])["message"] == "leaked <redacted>"
