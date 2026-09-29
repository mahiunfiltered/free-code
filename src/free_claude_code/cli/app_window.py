"""Open local FCC pages as chromeless, desktop-style app windows."""

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from free_claude_code.config.paths import config_dir_path

APP_WINDOW_PROFILE_DIRNAME = "chat-window"

_WINDOWS_BROWSERS = (
    ("msedge", r"Microsoft\Edge\Application\msedge.exe"),
    ("chrome", r"Google\Chrome\Application\chrome.exe"),
)
_WINDOWS_ROOT_ENV_VARS = ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")
_MACOS_APPS = ("Google Chrome", "Microsoft Edge")
_LINUX_BROWSERS = ("chromium", "chromium-browser", "google-chrome", "microsoft-edge")


def _chromium_flags(url: str) -> list[str]:
    # A dedicated profile gives the window its own taskbar entry and remembered size.
    profile_dir = config_dir_path() / APP_WINDOW_PROFILE_DIRNAME
    return [
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def _windows_browser() -> str | None:
    for command, relative_path in _WINDOWS_BROWSERS:
        found = shutil.which(command)
        if found:
            return found
        for env_var in _WINDOWS_ROOT_ENV_VARS:
            root = os.environ.get(env_var)
            if root and (Path(root) / relative_path).is_file():
                return str(Path(root) / relative_path)
    return None


def app_window_argv(url: str, platform: str = sys.platform) -> list[str] | None:
    """Return the argv that opens ``url`` as an app window, or None if unsupported."""

    if platform == "win32":
        browser = _windows_browser()
        return None if browser is None else [browser, *_chromium_flags(url)]
    if platform == "darwin":
        for app in _MACOS_APPS:
            if Path("/Applications", f"{app}.app").is_dir():
                return ["open", "-na", app, "--args", *_chromium_flags(url)]
        return None
    for command in _LINUX_BROWSERS:
        found = shutil.which(command)
        if found:
            return [found, *_chromium_flags(url)]
    return None


def open_app_window(url: str) -> bool:
    """Open ``url`` in a chromeless browser window, falling back to a normal tab."""

    argv = app_window_argv(url)
    if argv is None:
        return webbrowser.open(url)
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return webbrowser.open(url)
    return True
