"""Credential vault (ADR-014): AES-256-GCM file with an OS-protected 256-bit master key.

File format (``vault.bin``): ``b"FCCV1"`` magic, 12-byte random nonce, then
AES-256-GCM(key, nonce, aad=magic) over UTF-8 JSON ``{"version": 1, "secrets": {name: value}}``.
Secret names are encrypted too. Master key protection:

* Windows: DPAPI (CurrentUser) blob stored next to the vault (``vault.key``).
* macOS: login Keychain item via ``security -i`` (key sent on stdin, never argv).
* Linux: Secret Service via ``secret-tool`` (key sent on stdin).

There is no plaintext fallback: without a backend every operation raises ``VaultUnavailable``.
Secret values never appear in exceptions, ``repr`` or logs.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .interprocess_lock import InterprocessFileLock

MAGIC = b"FCCV1"
NONCE_BYTES = 12
KEY_BYTES = 32
KEYCHAIN_SERVICE = "free-claude-code-vault"
KEYCHAIN_ACCOUNT = "master-key"
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class VaultError(RuntimeError):
    """Base class; messages name paths and secret names, never values."""


class VaultUnavailable(VaultError):
    """No OS key-protection backend is usable."""


class VaultKeyMissing(VaultError):
    """The vault file exists but its master key cannot be found."""


class VaultCorrupted(VaultError):
    """The vault file is malformed, tampered with, or encrypted under another key."""


class SecretNotFound(VaultError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"Secret {name!r} is not in the vault. Add it with: fcc-secret set {name}"
        )
        self.name = name


class KeyProtector(Protocol):
    def load_key(self, *, create: bool) -> bytes:
        """Return the 32-byte master key; create it only when ``create`` is true."""
        ...


def validate_secret_name(name: str) -> str:
    if not SECRET_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid secret name {name!r}: use 1-128 letters, digits, '_', '.', '-'"
        )
    return name


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


# --- Windows DPAPI -----------------------------------------------------------

_DPAPI_ENTROPY = b"free-claude-code vault master key"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = (
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        )

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(data, len(data))
        return _Blob(
            len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
        ), buffer

    def _dpapi(data: bytes, *, protect: bool) -> bytes:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        data_in, _keep = _blob(data)
        entropy, _keep_entropy = _blob(_DPAPI_ENTROPY)
        data_out = _Blob()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = fn(
            ctypes.byref(data_in),
            None,
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(data_out),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            kernel32.LocalFree(data_out.pbData)

else:

    def _dpapi(data: bytes, *, protect: bool) -> bytes:
        raise VaultUnavailable("Windows DPAPI is only available on Windows")


class DpapiProtector:
    """Master key encrypted with DPAPI (CurrentUser scope) in ``key_path``."""

    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path

    def load_key(self, *, create: bool) -> bytes:
        if self.key_path.is_file():
            try:
                return _dpapi(self.key_path.read_bytes(), protect=False)
            except OSError as exc:
                raise VaultKeyMissing(
                    f"Cannot unprotect vault key {self.key_path} with DPAPI for this "
                    f"Windows user ({exc.__class__.__name__}). Was it created by another user?"
                ) from exc
        if not create:
            raise VaultKeyMissing(f"Vault master key file not found: {self.key_path}")
        key = secrets.token_bytes(KEY_BYTES)
        _atomic_write(self.key_path, _dpapi(key, protect=True))
        return key


# --- macOS Keychain / Linux Secret Service -------------------------------------


def _run(
    argv: list[str], stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            argv, input=stdin, capture_output=True, timeout=60, check=False
        )
    except FileNotFoundError as exc:
        raise VaultUnavailable(
            f"{argv[0]!r} not found; cannot protect the vault key"
        ) from exc


def _decode_key(text: bytes, source: str) -> bytes:
    try:
        key = bytes.fromhex(text.decode("ascii").strip())
    except ValueError as exc:
        raise VaultCorrupted(f"{source} returned a malformed master key") from exc
    if len(key) != KEY_BYTES:
        raise VaultCorrupted(f"{source} returned a master key of the wrong size")
    return key


class KeychainProtector:
    """macOS login Keychain generic password (UNVERIFIED: no macOS host in CI)."""

    _NOT_FOUND = 44  # errSecItemNotFound exit status of `security`

    def load_key(self, *, create: bool) -> bytes:
        found = _run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ]
        )
        if found.returncode == 0:
            return _decode_key(found.stdout, "macOS Keychain")
        if found.returncode != self._NOT_FOUND:
            raise VaultUnavailable(
                f"macOS Keychain lookup failed (exit {found.returncode})"
            )
        if not create:
            raise VaultKeyMissing("Vault master key not found in the macOS Keychain")
        key = secrets.token_bytes(KEY_BYTES)
        # `security -i` reads commands from stdin, keeping the key off argv.
        command = f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {KEYCHAIN_ACCOUNT} -w {key.hex()}\n"
        stored = _run(["security", "-i"], command.encode("ascii"))
        if stored.returncode != 0:
            raise VaultUnavailable(
                f"macOS Keychain store failed (exit {stored.returncode})"
            )
        return key


class SecretToolProtector:
    """Linux Secret Service via libsecret's ``secret-tool`` (UNVERIFIED: needs a keyring daemon)."""

    _ATTRS = ("service", KEYCHAIN_SERVICE, "account", KEYCHAIN_ACCOUNT)

    def load_key(self, *, create: bool) -> bytes:
        found = _run(["secret-tool", "lookup", *self._ATTRS])
        if found.returncode == 0 and found.stdout.strip():
            return _decode_key(found.stdout, "Secret Service")
        # ponytail: secret-tool exits 1 for both "missing" and some errors; stderr disambiguates.
        if found.returncode not in (0, 1) or found.stderr.strip():
            raise VaultUnavailable(
                f"secret-tool lookup failed (exit {found.returncode})"
            )
        if not create:
            raise VaultKeyMissing(
                "Vault master key not found in the Secret Service keyring"
            )
        key = secrets.token_bytes(KEY_BYTES)
        stored = _run(
            [
                "secret-tool",
                "store",
                "--label=Free Claude Code vault key",
                *self._ATTRS,
            ],
            key.hex().encode("ascii"),
        )
        if stored.returncode != 0:
            raise VaultUnavailable(
                f"secret-tool store failed (exit {stored.returncode})"
            )
        return key


def platform_protector(key_path: Path) -> KeyProtector:
    """Return this OS's key protector, or raise an actionable ``VaultUnavailable``."""

    if sys.platform == "win32":
        return DpapiProtector(key_path)
    if sys.platform == "darwin":
        if shutil.which("security") is None:
            raise VaultUnavailable(
                "macOS `security` CLI not found; Keychain unavailable"
            )
        return KeychainProtector()
    if shutil.which("secret-tool") is None:
        raise VaultUnavailable(
            "No OS keyring for the credential vault: install libsecret-tools "
            "(Debian/Ubuntu) or libsecret (Fedora) and run a Secret Service daemon, "
            "or keep secrets as plain .env values."
        )
    return SecretToolProtector()


# --- Vault ---------------------------------------------------------------------


class Vault:
    """Encrypted name -> secret store. Every call re-reads the file (other processes may write)."""

    def __init__(self, path: Path, protector: KeyProtector) -> None:
        self.path = Path(path)
        self._protector = protector
        self._key: bytes | None = None
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    def __repr__(self) -> str:
        return f"Vault(path={str(self.path)!r})"

    def _master_key(self, *, create: bool) -> bytes:
        if self._key is None:
            key = self._protector.load_key(create=create)
            if len(key) != KEY_BYTES:
                raise VaultCorrupted(
                    "Key protector returned a key that is not 256 bits"
                )
            self._key = key
        return self._key

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise VaultUnavailable(
                f"Cannot read vault file {self.path}: {exc.strerror}"
            ) from exc
        if len(raw) < len(MAGIC) + NONCE_BYTES + 16 or not raw.startswith(MAGIC):
            raise VaultCorrupted(
                f"Vault file {self.path} is not a FCC vault or is truncated"
            )
        nonce = raw[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
        try:
            plain = AESGCM(self._master_key(create=False)).decrypt(
                nonce, raw[len(MAGIC) + NONCE_BYTES :], MAGIC
            )
        except InvalidTag:
            raise VaultCorrupted(
                f"Vault file {self.path} failed its integrity check (tampered, corrupted, "
                "or encrypted under a different master key)"
            ) from None
        data = json.loads(plain)
        found = data.get("secrets") if isinstance(data, dict) else None
        if not isinstance(found, dict) or not all(
            isinstance(v, str) for v in found.values()
        ):
            raise VaultCorrupted(f"Vault file {self.path} has an unexpected structure")
        return found

    def _write(self, entries: dict[str, str]) -> None:
        # Mint a key only when no vault exists yet; otherwise a lookup miss would orphan secrets.
        key = self._master_key(create=not self.path.exists())
        nonce = secrets.token_bytes(NONCE_BYTES)
        plain = json.dumps({"version": 1, "secrets": entries}, sort_keys=True).encode()
        _atomic_write(
            self.path, MAGIC + nonce + AESGCM(key).encrypt(nonce, plain, MAGIC)
        )

    def _locked(self) -> InterprocessFileLock:
        return InterprocessFileLock(self._lock_path)

    def set(self, name: str, value: str) -> None:
        validate_secret_name(name)
        if not value:
            raise ValueError(f"Secret {name!r} must not be empty")
        with self._locked():
            entries = self._read()
            entries[name] = value
            self._write(entries)

    def get(self, name: str) -> str:
        validate_secret_name(name)
        entries = self._read()
        if name not in entries:
            raise SecretNotFound(name)
        return entries[name]

    def delete(self, name: str) -> bool:
        validate_secret_name(name)
        with self._locked():
            entries = self._read()
            if entries.pop(name, None) is None:
                return False
            self._write(entries)
            return True

    def names(self) -> list[str]:
        return sorted(self._read())
