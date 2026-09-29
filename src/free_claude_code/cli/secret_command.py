"""``fcc-secret``: manage the credential vault. Secret values are never accepted on argv
and never printed; the admin API uses the small functions below (names only)."""

import argparse
import getpass
import os
import re
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from free_claude_code.config.env_files import dotenv_values_from_file
from free_claude_code.config.env_migrations import atomic_write_managed_config
from free_claude_code.config.loader import (
    VAULT_REF_PREFIX,
    clear_settings_cache,
    managed_vault,
)
from free_claude_code.config.paths import config_lock_path, managed_env_path
from free_claude_code.core.interprocess_lock import InterprocessFileLock
from free_claude_code.core.vault import VaultError, validate_secret_name

MIGRATABLE_KEY_RE = re.compile(r"^[A-Z0-9_]+_API_KEYS?$")


def set_secret(name: str, value: str) -> None:
    managed_vault().set(name, value)
    clear_settings_cache()


def list_secrets() -> list[str]:
    return managed_vault().names()


def delete_secret(name: str) -> bool:
    deleted = managed_vault().delete(name)
    clear_settings_cache()
    return deleted


def migrate_env(env_path: Path | None = None) -> tuple[list[str], Path | None]:
    """Move plaintext ``*_API_KEY(S)`` values from the managed .env into the vault.

    Returns (migrated keys, backup path). The backup still holds the plaintext values.
    """

    path = env_path or managed_env_path()
    if not path.is_file():
        return [], None
    lock = InterprocessFileLock(config_lock_path())
    if not lock.acquire(wait=True, timeout=10.0):
        raise TimeoutError(
            f"Could not acquire managed-config lock: {config_lock_path()}"
        )
    try:
        values = dotenv_values_from_file(path)
        targets = {
            key: value
            for key, value in values.items()
            if MIGRATABLE_KEY_RE.match(key)
            and value.strip()
            and not value.strip().startswith(VAULT_REF_PREFIX)
        }
        if not targets:
            return [], None
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        if os.name != "nt":
            backup.chmod(0o600)
        vault = managed_vault()
        for key, value in targets.items():
            vault.set(key, value)
        # Only rewrite once every value is safely in the vault.
        rewritten = {**values, **{key: f"{VAULT_REF_PREFIX}{key}" for key in targets}}
        atomic_write_managed_config(rewritten, path=path)
    finally:
        lock.release()
    clear_settings_cache()
    return sorted(targets), backup


def _read_value(name: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(f"Value for {name} (hidden): ")
    return sys.stdin.read().rstrip("\r\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fcc-secret", description="Manage the FCC credential vault."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    set_parser = sub.add_parser(
        "set", help="store a secret (value via hidden prompt or stdin)"
    )
    set_parser.add_argument("name")
    set_parser.add_argument("value", nargs="*", help=argparse.SUPPRESS)
    sub.add_parser("list", help="list secret names")
    delete_parser = sub.add_parser("delete", help="delete a secret")
    delete_parser.add_argument("name")
    sub.add_parser(
        "migrate", help="move plaintext *_API_KEY(S) from ~/.fcc/.env into the vault"
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "set":
            if args.value:
                print(
                    "Refusing a secret value on the command line (it would leak into "
                    "shell history and process lists). Pipe it on stdin or omit it "
                    "to be prompted.",
                    file=sys.stderr,
                )
                return 2
            name = validate_secret_name(args.name)
            value = _read_value(name)
            if not value:
                print("No value provided; nothing stored.", file=sys.stderr)
                return 2
            set_secret(name, value)
            print(f"Stored {name}. Reference it in .env as {name}=vault:{name}")
        elif args.command == "list":
            for name in list_secrets():
                print(name)
        elif args.command == "delete":
            if not delete_secret(validate_secret_name(args.name)):
                print(f"No secret named {args.name}", file=sys.stderr)
                return 1
            print(f"Deleted {args.name}")
        else:
            migrated, backup = migrate_env()
            if not migrated:
                print("No plaintext *_API_KEY values to migrate.")
                return 0
            print(
                f"Moved {len(migrated)} value(s) into the vault: {', '.join(migrated)}"
            )
            print(f"Backup with the old plaintext values: {backup}")
            print("Delete the backup once FCC starts correctly.")
    except (VaultError, ValueError, OSError, TimeoutError) as exc:
        print(f"fcc-secret: {exc}", file=sys.stderr)
        return 1
    return 0


def launch() -> None:
    raise SystemExit(main())
