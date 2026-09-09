"""Tests for Windows console mode hardening, Win32 clipboard integration, and interactive prompt engine."""

import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from free_claude_code.cli.interactive_input import (
    MultilineBuffer,
    configure_windows_console_modes,
    diagnose_terminal_input,
    get_clipboard_text,
    get_windows_clipboard_text,
    print_diagnostics,
    set_windows_clipboard_text,
)
from free_claude_code.cli.launchers.claude import launch


def test_multiline_buffer_basic_operations() -> None:
    buf = MultilineBuffer()
    assert buf.is_empty
    assert buf.line_count == 1
    assert buf.get_text() == ""

    # Insert single chars
    for ch in "hello":
        buf.insert_char(ch)
    assert buf.get_text() == "hello"
    assert buf.cursor_row == 0
    assert buf.cursor_col == 5

    # Split line
    buf.split_line()
    assert buf.line_count == 2
    assert buf.cursor_row == 1
    assert buf.cursor_col == 0

    # Insert second line
    for ch in "world":
        buf.insert_char(ch)
    assert buf.get_text() == "hello\nworld"
    assert buf.cursor_row == 1
    assert buf.cursor_col == 5


def test_multiline_buffer_insert_multiline_text() -> None:
    buf = MultilineBuffer()
    sample_text = (
        "# Plan\n"
        "```python\n"
        "def test_func():\n"
        "    return 'success'\n"
        "```\n"
        "End of prompt."
    )
    buf.insert_text(sample_text)
    assert buf.get_text() == sample_text
    assert buf.line_count == 6
    assert not buf.is_empty


def test_multiline_buffer_insert_crlf_normalized() -> None:
    buf = MultilineBuffer()
    crlf_text = "line 1\r\nline 2\r\nline 3"
    buf.insert_text(crlf_text)
    assert buf.get_text() == "line 1\nline 2\nline 3"
    assert buf.line_count == 3


def test_multiline_buffer_backspace_and_delete() -> None:
    buf = MultilineBuffer()
    buf.insert_text("first\nsecond")
    assert buf.get_text() == "first\nsecond"
    assert buf.cursor_row == 1
    assert buf.cursor_col == 6

    # Backspace last char 'd'
    assert buf.delete_backspace()
    assert buf.get_text() == "first\nsecon"

    # Move to start of second line and backspace to join lines
    buf.cursor_col = 0
    assert buf.delete_backspace()
    assert buf.get_text() == "firstsecon"
    assert buf.cursor_row == 0
    assert buf.cursor_col == 5

    # Delete forward
    assert buf.delete_forward()
    assert buf.get_text() == "firstecon"


def test_multiline_buffer_cursor_navigation() -> None:
    buf = MultilineBuffer()
    buf.insert_text("line one\nline two\nline three")

    buf.move_to_buffer_start()
    assert buf.cursor_row == 0
    assert buf.cursor_col == 0

    buf.move_to_line_end()
    assert buf.cursor_col == 8

    buf.move_cursor_down()
    assert buf.cursor_row == 1
    assert buf.cursor_col == 8

    buf.move_cursor_left()
    assert buf.cursor_col == 7

    buf.move_to_buffer_end()
    assert buf.cursor_row == 2
    assert buf.cursor_col == 10


def test_multiline_buffer_clear_operations() -> None:
    buf = MultilineBuffer()
    buf.insert_text("line 1\nline 2\nline 3")

    buf.cursor_row = 1
    buf.cursor_col = 3
    buf.clear_to_end_of_line()
    assert buf.lines[1] == "lin"

    buf.clear_line()
    assert buf.lines[1] == ""

    buf.clear()
    assert buf.is_empty
    assert buf.line_count == 1
    assert buf.get_text() == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 clipboard test")
def test_windows_clipboard_loopback() -> None:
    test_string = "Free Claude Code Win32 Clipboard Verification: 🚀 123 \n Line 2"
    success = False
    for _ in range(10):
        if set_windows_clipboard_text(test_string):
            extracted = get_windows_clipboard_text()
            if extracted == test_string:
                success = True
                break
        time.sleep(0.05)
    assert success


def test_diagnose_terminal_input_structure() -> None:
    diag = diagnose_terminal_input()
    assert "platform" in diag
    assert "streams" in diag
    assert "environment" in diag
    assert "clipboard" in diag
    assert isinstance(diag["clipboard"], dict)
    assert "available" in diag["clipboard"]


def test_print_diagnostics_output() -> None:
    diag = diagnose_terminal_input()
    out = io.StringIO()
    print_diagnostics(diag, file=out)
    output = out.getvalue()
    assert "FREE CLAUDE CODE - TERMINAL & INPUT DIAGNOSTICS" in output
    assert "[Platform & Environment]" in output
    assert "[Standard Streams & Encoding]" in output
    assert "Diagnostic check completed successfully." in output


def test_claude_launcher_diagnose_input_flag() -> None:
    out = io.StringIO()
    with patch("sys.stdout", out):
        launch(["--diagnose-input"])
    output = out.getvalue()
    assert "FREE CLAUDE CODE - TERMINAL & INPUT DIAGNOSTICS" in output


def test_claude_launcher_paste_flag_with_clipboard() -> None:
    test_clip_prompt = "Refactor this module from clipboard"
    with (
        patch(
            "free_claude_code.cli.launchers.claude.get_settings"
        ) as mock_settings,
        patch(
            "free_claude_code.cli.launchers.claude.preflight_proxy",
            return_value=None,
        ),
        patch(
            "free_claude_code.cli.launchers.claude.resolve_client_binary",
            return_value="claude",
        ),
        patch(
            "free_claude_code.cli.interactive_input.get_clipboard_text",
            return_value=test_clip_prompt,
        ),
        patch(
            "free_claude_code.cli.launchers.claude.run_client_process"
        ) as mock_run,
    ):
        mock_settings.return_value.proxy_auth_token = "tok"
        launch(["--paste"])
        mock_run.assert_called_once()
        cmd = mock_run.call_args.kwargs["command"]
        assert "-p" in cmd
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == test_clip_prompt


def test_configure_windows_console_modes_non_windows_safe() -> None:
    if sys.platform != "win32":
        info = configure_windows_console_modes()
        assert info["configured"] is False
        assert info["reason"] == "not_windows"


def test_multiline_buffer_huge_text_performance() -> None:
    buf = MultilineBuffer()
    # 100KB multiline text
    chunk = "def calculate_hash(data: str) -> str:\n    return hashlib.sha256(data.encode()).hexdigest()\n"
    huge_prompt = chunk * 1000
    assert len(huge_prompt) > 80_000

    buf.insert_text(huge_prompt)
    assert buf.line_count > 2000
    assert buf.get_text() == huge_prompt


def test_interactive_prompt_reader_posix_fallback() -> None:
    from free_claude_code.cli.interactive_input import InteractivePromptReader

    reader = InteractivePromptReader(prompt_prefix="test> ")
    with patch("builtins.input", return_value="user prompt text"):
        res = reader._read_posix_fallback()
        assert res == "user prompt text"


def test_claude_launcher_interactive_prompt_flag() -> None:
    interactive_prompt = "Interactive multiline prompt\nLine 2"
    with (
        patch(
            "free_claude_code.cli.launchers.claude.get_settings"
        ) as mock_settings,
        patch(
            "free_claude_code.cli.launchers.claude.preflight_proxy",
            return_value=None,
        ),
        patch(
            "free_claude_code.cli.launchers.claude.resolve_client_binary",
            return_value="claude",
        ),
        patch(
            "free_claude_code.cli.interactive_input.InteractivePromptReader.read_prompt",
            return_value=interactive_prompt,
        ),
        patch(
            "free_claude_code.cli.launchers.claude.run_client_process"
        ) as mock_run,
    ):
        mock_settings.return_value.proxy_auth_token = "tok"
        launch(["--prompt"])
        mock_run.assert_called_once()
        cmd = mock_run.call_args.kwargs["command"]
        assert "-p" in cmd
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == interactive_prompt


def test_inject_text_into_console_mocked() -> None:
    from free_claude_code.cli.interactive_input import inject_text_into_console

    if sys.platform != "win32":
        assert inject_text_into_console("test") == 0
    else:
        # On Windows, test with invalid/mock handle returns 0 without crashing
        res = inject_text_into_console("")
        assert res == 0


def test_windows_console_paste_bridge_lifecycle() -> None:
    from free_claude_code.cli.interactive_input import (
        WindowsConsolePasteBridge,
    )

    bridge = WindowsConsolePasteBridge()
    assert not bridge.is_running
    if sys.platform == "win32":
        started = bridge.start()
        assert started is True
        assert bridge.is_running is True
        bridge.stop()
        assert not bridge.is_running
    else:
        assert bridge.start() is False


def test_required_multiline_prompt_from_user_spec() -> None:
    required_prompt = (
        "Analyze the current Claude Code project.\n\n"
        "This is a multiline prompt.\n\n"
        "Inspect:\n\n"
        "D:\\Claude code\n\n"
        "Then inspect:\n\n"
        "src/\n"
        "  app/\n"
        "    main.py\n"
        "    config.py\n\n"
        "Code:\n\n"
        "```python\n"
        "def hello():\n"
        "    print('Hello')\n"
        "```"
    )

    buf = MultilineBuffer()
    buf.insert_text(required_prompt)
    assert buf.get_text() == required_prompt
    assert buf.line_count == len(required_prompt.split("\n"))
    assert any("def hello():" in line for line in buf.lines)


def test_claude_launcher_legacy_console_paste_flag() -> None:
    with (
        patch("free_claude_code.cli.launchers.claude.get_settings") as mock_settings,
        patch("free_claude_code.cli.launchers.claude.preflight_proxy", return_value=None),
        patch("free_claude_code.cli.launchers.claude.resolve_client_binary", return_value="claude"),
        patch("free_claude_code.cli.launchers.claude.run_client_process") as mock_run,
    ):
        mock_settings.return_value.proxy_auth_token = "tok"
        launch(["--legacy-console-paste"])

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["legacy_console_paste"] is True




