from __future__ import annotations

"""Shared process helpers for installed client CLI launchers."""

import shutil
import subprocess
import sys
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request

from free_claude_code.cli.local_http import open_local_request
from free_claude_code.cli.process_registry import (
    kill_pid_tree_best_effort,
    register_pid,
    unregister_pid,
)

PROXY_PREFLIGHT_PATH = "/health"
PROXY_PREFLIGHT_TIMEOUT_SECONDS = 1.5


def proxy_v1_url(proxy_root_url: str) -> str:
    """Return the canonical local proxy API root for client launchers."""

    stripped = proxy_root_url.rstrip("/")
    return stripped if stripped.endswith("/v1") else f"{stripped}/v1"


def preflight_proxy(proxy_root_url: str) -> str | None:
    """Return an error message when the local proxy health check is unreachable."""

    url = f"{proxy_root_url.rstrip('/')}{PROXY_PREFLIGHT_PATH}"
    request = Request(url, method="GET")
    try:
        with open_local_request(
            request, timeout=PROXY_PREFLIGHT_TIMEOUT_SECONDS
        ) as response:
            status_code = response.status
    except HTTPError as exc:
        return f"returned HTTP {exc.code}"
    except URLError as exc:
        return str(exc.reason)
    except OSError as exc:
        return str(exc)

    if not 200 <= status_code < 300:
        return f"returned HTTP {status_code}"
    return None


def resolve_client_binary(
    *,
    binary_name: str,
    display_name: str,
    install_hint: str,
) -> str:
    """Resolve an installed client binary or exit with a user-facing hint."""

    client_command = shutil.which(binary_name)
    if client_command is None:
        print(
            f"Could not find {display_name} command: {binary_name}",
            file=sys.stderr,
        )
        print(install_hint, file=sys.stderr)
        raise SystemExit(127)
    return client_command


def run_client_process(
    *,
    command: list[str],
    env: Mapping[str, str],
    binary_name: str,
    display_name: str,
    install_hint: str,
    legacy_console_paste: bool = False,
    stdin_payload: str | None = None,
) -> None:
    """Run a client CLI command and mirror its exit code with large prompt and isolation support."""

    import signal
    import threading

    process: subprocess.Popen[bytes] | None = None
    old_sigint = None
    paste_bridge = None
    
    # Check if a large prompt is passed via -p / --print on Windows (> 2048 chars)
    # If so, convert it to stdin delivery to prevent exceeding the 32,767 char cmdline limit
    effective_command = list(command)
    effective_stdin = stdin_payload
    
    for flag in ("-p", "--print"):
        if flag in effective_command:
            idx = effective_command.index(flag)
            if idx + 1 < len(effective_command):
                prompt_val = effective_command[idx + 1]
                if len(prompt_val) > 2048 or (sys.platform == "win32" and "\n" in prompt_val and len(prompt_val) > 1024):
                    # Extract to stdin
                    if effective_stdin is None:
                        effective_stdin = prompt_val
                        effective_command.pop(idx + 1)
                        effective_command.pop(idx)
                    break

    try:
        if sys.platform == "win32":
            # On Windows, configure console modes for UTF-8 and VT I/O
            try:
                from free_claude_code.cli.interactive_input import (
                    configure_windows_console_modes,
                )

                configure_windows_console_modes()
            except Exception:
                pass

            # Only run the legacy paste bridge if explicitly requested (e.g. legacy fallback)
            if legacy_console_paste:
                try:
                    from free_claude_code.cli.interactive_input import (
                        WindowsConsolePasteBridge,
                    )

                    paste_bridge = WindowsConsolePasteBridge()
                    paste_bridge.start()
                except Exception:
                    paste_bridge = None

            # On Windows, ignore SIGINT in the parent wrapper while the interactive client process runs,
            # allowing the client (e.g. Claude Code, Codex) to handle Ctrl+C (cancel generation vs copy) directly.
            try:
                old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
            except (ValueError, OSError):
                old_sigint = None

        popen_kwargs: dict = {
            "env": dict(env),
        }
        if effective_stdin is not None:
            popen_kwargs["stdin"] = subprocess.PIPE

        process = subprocess.Popen(effective_command, **popen_kwargs)
        if process.pid:
            register_pid(process.pid)

        # Stream stdin payload if present
        if effective_stdin is not None and process.stdin:
            def _write_stdin():
                try:
                    process.stdin.write(effective_stdin.encode("utf-8"))
                    process.stdin.flush()
                    process.stdin.close()
                except Exception:
                    pass
            t = threading.Thread(target=_write_stdin, daemon=True)
            t.start()

        return_code = process.wait()
    except FileNotFoundError:
        print(
            f"Could not find {display_name} command: {binary_name}",
            file=sys.stderr,
        )
        print(install_hint, file=sys.stderr)
        raise SystemExit(127) from None
    except KeyboardInterrupt:
        if process is not None and process.pid:
            kill_pid_tree_best_effort(process.pid)
        raise
    finally:
        if paste_bridge is not None:
            try:
                paste_bridge.stop()
            except Exception:
                pass
        if old_sigint is not None:
            try:
                signal.signal(signal.SIGINT, old_sigint)
            except (ValueError, OSError):
                pass
        if process is not None and process.pid:
            unregister_pid(process.pid)

    raise SystemExit(return_code)
