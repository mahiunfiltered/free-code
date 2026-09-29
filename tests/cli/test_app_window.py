"""Chromeless app-window launcher contracts (no real browsers are launched)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from free_claude_code.cli import app_window

URL = "http://127.0.0.1:8082/chat"
EDGE = r"Microsoft\Edge\Application\msedge.exe"
CHROME = r"Google\Chrome\Application\chrome.exe"


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {}
    for env_var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = tmp_path / env_var.replace("(", "_").replace(")", "_")
        root.mkdir()
        monkeypatch.setenv(env_var, str(root))
        paths[env_var] = root
    monkeypatch.setattr(app_window, "config_dir_path", lambda: tmp_path / ".fcc")
    return paths


def _install(root: Path, relative: str) -> Path:
    exe = root / relative
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    return exe


def test_windows_prefers_edge_over_chrome(roots: dict[str, Path]) -> None:
    chrome = _install(roots["LOCALAPPDATA"], CHROME)
    edge = _install(roots["ProgramFiles"], EDGE)
    with patch.object(app_window.shutil, "which", return_value=None):
        argv = app_window.app_window_argv(URL, "win32")

    assert argv is not None
    assert argv[0] == str(edge)
    assert f"--app={URL}" in argv
    assert any(
        arg.startswith("--user-data-dir=") and "chat-window" in arg for arg in argv
    )
    assert str(chrome) not in argv


def test_windows_falls_back_to_chrome_in_localappdata(roots: dict[str, Path]) -> None:
    chrome = _install(roots["LOCALAPPDATA"], CHROME)
    with patch.object(app_window.shutil, "which", return_value=None):
        argv = app_window.app_window_argv(URL, "win32")

    assert argv is not None
    assert argv[0] == str(chrome)


def test_windows_uses_browser_on_path_first(roots: dict[str, Path]) -> None:
    _install(roots["ProgramFiles"], EDGE)
    with patch.object(app_window.shutil, "which", side_effect=["C:/path/msedge.exe"]):
        argv = app_window.app_window_argv(URL, "win32")

    assert argv is not None
    assert argv[0] == "C:/path/msedge.exe"


def test_windows_without_browsers_returns_none(roots: dict[str, Path]) -> None:
    with patch.object(app_window.shutil, "which", return_value=None):
        assert app_window.app_window_argv(URL, "win32") is None


def test_linux_order_and_app_flag(roots: dict[str, Path]) -> None:
    found = {"google-chrome": "/usr/bin/google-chrome", "microsoft-edge": "/x"}
    with patch.object(app_window.shutil, "which", side_effect=found.get):
        argv = app_window.app_window_argv(URL, "linux")

    assert argv is not None
    assert argv[0] == "/usr/bin/google-chrome"
    assert f"--app={URL}" in argv


def test_macos_uses_open_with_app_args(roots: dict[str, Path]) -> None:
    with patch.object(app_window.Path, "is_dir", return_value=True):
        argv = app_window.app_window_argv(URL, "darwin")

    assert argv is not None
    assert argv[:5] == ["open", "-na", "Google Chrome", "--args", f"--app={URL}"]


def test_open_app_window_launches_argv() -> None:
    argv = ["msedge.exe", f"--app={URL}"]
    with (
        patch.object(app_window, "app_window_argv", return_value=argv),
        patch.object(app_window.subprocess, "Popen") as popen,
        patch.object(app_window.webbrowser, "open") as browser_open,
    ):
        assert app_window.open_app_window(URL) is True

    assert popen.call_args.args[0] == argv
    browser_open.assert_not_called()


def test_open_app_window_falls_back_to_webbrowser_without_browser() -> None:
    with (
        patch.object(app_window, "app_window_argv", return_value=None),
        patch.object(app_window.subprocess, "Popen") as popen,
        patch.object(app_window.webbrowser, "open", return_value=True) as browser_open,
    ):
        assert app_window.open_app_window(URL) is True

    popen.assert_not_called()
    browser_open.assert_called_once_with(URL)


def test_open_app_window_falls_back_when_launch_fails() -> None:
    with (
        patch.object(app_window, "app_window_argv", return_value=["missing.exe"]),
        patch.object(app_window.subprocess, "Popen", side_effect=OSError),
        patch.object(app_window.webbrowser, "open", return_value=True) as browser_open,
    ):
        assert app_window.open_app_window(URL) is True

    browser_open.assert_called_once_with(URL)
