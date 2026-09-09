"""Windows console mode hardening, Win32 clipboard integration, and interactive multiline prompt engine."""

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import TextIO

# Win32 Constants
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
GHND = GMEM_MOVEABLE | GMEM_ZEROINIT

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12

# Console Input Modes
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_WINDOW_INPUT = 0x0008
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_INSERT_MODE = 0x0020
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
ENABLE_AUTO_POSITION = 0x0100
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

# Console Output Modes
ENABLE_PROCESSED_OUTPUT = 0x0001
ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
DISABLE_NEWLINE_AUTO_RETURN = 0x0008
ENABLE_LVB_GRID_WORLDWIDE = 0x0010

# Win32 Hook & Virtual Key Constants
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012
WM_NULL = 0x0000

VK_V = 0x56
VK_INSERT = 0x2D
VK_CONTROL = 0x11
VK_SHIFT = 0x10


if sys.platform == "win32":
    import ctypes.wintypes

    class CHAR_UNION(ctypes.Union):
        _fields_ = [
            ("UnicodeChar", ctypes.wintypes.WCHAR),
            ("AsciiChar", ctypes.c_char),
        ]

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", ctypes.wintypes.BOOL),
            ("wRepeatCount", ctypes.wintypes.WORD),
            ("wVirtualKeyCode", ctypes.wintypes.WORD),
            ("wVirtualScanCode", ctypes.wintypes.WORD),
            ("uChar", CHAR_UNION),
            ("dwControlKeyState", ctypes.wintypes.DWORD),
        ]

    class EVENT_UNION(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [
            ("EventType", ctypes.wintypes.WORD),
            ("Event", EVENT_UNION),
        ]

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", ctypes.wintypes.DWORD),
            ("scanCode", ctypes.wintypes.DWORD),
            ("flags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_int,
        ctypes.wintypes.WPARAM,
        ctypes.POINTER(KBDLLHOOKSTRUCT),
    )
else:
    HOOKPROC = object  # type: ignore


def _setup_win32_signatures(user32: ctypes.WinDLL, kernel32: ctypes.WinDLL) -> None:
    """Ensure proper 64-bit and 32-bit Win32 function signatures on ctypes DLLs."""
    import ctypes.wintypes

    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p

    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p

    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int

    kernel32.GetStdHandle.argtypes = [ctypes.c_int]
    kernel32.GetStdHandle.restype = ctypes.c_void_p

    kernel32.GetConsoleMode.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.GetConsoleMode.restype = ctypes.c_int

    kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetConsoleMode.restype = ctypes.c_int

    kernel32.SetConsoleCP.argtypes = [ctypes.c_uint]
    kernel32.SetConsoleCP.restype = ctypes.c_int

    kernel32.SetConsoleOutputCP.argtypes = [ctypes.c_uint]
    kernel32.SetConsoleOutputCP.restype = ctypes.c_int

    kernel32.GetConsoleWindow.argtypes = []
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p

    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = ctypes.wintypes.DWORD

    kernel32.WriteConsoleInputW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    kernel32.WriteConsoleInputW.restype = ctypes.wintypes.BOOL

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int

    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_int

    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_int

    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_int

    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p

    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int,
        HOOKPROC,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
    ]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p

    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = ctypes.c_int

    user32.CallNextHookEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.wintypes.WPARAM,
        ctypes.POINTER(KBDLLHOOKSTRUCT),
    ]
    user32.CallNextHookEx.restype = ctypes.c_long

    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p

    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    user32.PostThreadMessageW.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.c_uint,
        ctypes.wintypes.WPARAM,
        ctypes.wintypes.LPARAM,
    ]
    user32.PostThreadMessageW.restype = ctypes.c_int

    user32.MsgWaitForMultipleObjects.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
    ]
    user32.MsgWaitForMultipleObjects.restype = ctypes.wintypes.DWORD


def get_windows_clipboard_text(
    max_retries: int = 20, retry_delay: float = 0.03
) -> str | None:
    """Read full unicode text from Windows clipboard via Win32 user32 API with retry on contention."""
    if sys.platform != "win32":
        return None

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        _setup_win32_signatures(user32, kernel32)
    except (AttributeError, OSError):
        return None

    import random

    opened = False
    for attempt in range(max_retries):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(retry_delay + random.uniform(0.01, 0.04))

    if not opened:
        return None

    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None

        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None

        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None

        try:
            text = ctypes.cast(ptr, ctypes.c_wchar_p).value
            if text is None:
                return None
            return text.replace("\r\n", "\n").replace("\r", "\n")
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_windows_clipboard_text(
    text: str, max_retries: int = 20, retry_delay: float = 0.03
) -> bool:
    """Write unicode text to Windows clipboard via Win32 user32 API."""
    if sys.platform != "win32":
        return False

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        _setup_win32_signatures(user32, kernel32)
    except (AttributeError, OSError):
        return False

    import random

    opened = False
    for attempt in range(max_retries):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(retry_delay + random.uniform(0.01, 0.04))

    if not opened:
        return False

    try:
        user32.EmptyClipboard()
        encoded = (text + "\0").encode("utf-16le")
        buffer_size = len(encoded)

        h_mem = kernel32.GlobalAlloc(GHND, buffer_size)
        if not h_mem:
            return False

        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            return False

        try:
            ctypes.memmove(ptr, encoded, buffer_size)
        finally:
            kernel32.GlobalUnlock(h_mem)

        if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            return False
        return True
    finally:
        user32.CloseClipboard()


def get_clipboard_text() -> str | None:
    """Get text from system clipboard across Windows, macOS, and Linux."""
    if sys.platform == "win32":
        return get_windows_clipboard_text()

    if sys.platform == "darwin":
        try:
            res = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=2.0
            )
            if res.returncode == 0:
                return res.stdout.replace("\r\n", "\n").replace("\r", "\n")
        except Exception:
            pass
        return None

    for tool, args in [
        ("wl-paste", []),
        ("xclip", ["-selection", "clipboard", "-o"]),
        ("xsel", ["--clipboard", "--output"]),
    ]:
        if shutil.which(tool):
            try:
                res = subprocess.run(
                    [tool, *args], capture_output=True, text=True, timeout=2.0
                )
                if res.returncode == 0:
                    return res.stdout.replace("\r\n", "\n").replace("\r", "\n")
            except Exception:
                pass
    return None


def configure_windows_console_modes() -> dict[str, object]:
    """Configure Windows console modes for Virtual Terminal Input/Output, QuickEdit, and UTF-8 code page."""
    if sys.platform != "win32":
        return {"configured": False, "reason": "not_windows"}

    info: dict[str, object] = {"configured": True}
    try:
        kernel32 = ctypes.windll.kernel32
        h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        h_stdout = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

        try:
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
            info["codepage"] = 65001
        except Exception:
            info["codepage"] = 0

        # Input mode
        in_mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h_stdin, ctypes.byref(in_mode)):
            info["initial_in_mode"] = hex(in_mode.value)
            new_in_mode = (
                in_mode.value
                | ENABLE_PROCESSED_INPUT
                | ENABLE_QUICK_EDIT_MODE
                | ENABLE_EXTENDED_FLAGS
                | ENABLE_VIRTUAL_TERMINAL_INPUT
            )
            kernel32.SetConsoleMode(h_stdin, new_in_mode)
            updated_in_mode = ctypes.c_uint32()
            kernel32.GetConsoleMode(h_stdin, ctypes.byref(updated_in_mode))
            info["configured_in_mode"] = hex(updated_in_mode.value)
            info["vt_input_enabled"] = bool(
                updated_in_mode.value & ENABLE_VIRTUAL_TERMINAL_INPUT
            )
            info["quick_edit_enabled"] = bool(
                updated_in_mode.value & ENABLE_QUICK_EDIT_MODE
            )

        # Output mode
        out_mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h_stdout, ctypes.byref(out_mode)):
            info["initial_out_mode"] = hex(out_mode.value)
            new_out_mode = (
                out_mode.value
                | ENABLE_PROCESSED_OUTPUT
                | ENABLE_WRAP_AT_EOL_OUTPUT
                | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
            kernel32.SetConsoleMode(h_stdout, new_out_mode)
            updated_out_mode = ctypes.c_uint32()
            kernel32.GetConsoleMode(h_stdout, ctypes.byref(updated_out_mode))
            info["configured_out_mode"] = hex(updated_out_mode.value)
            info["vt_output_enabled"] = bool(
                updated_out_mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )

        return info
    except Exception as exc:
        return {"configured": False, "error": str(exc)}


@dataclass
class MultilineBuffer:
    """2D text buffer with line/column tracking and editing operations."""

    lines: list[str] = field(default_factory=lambda: [""])
    cursor_row: int = 0
    cursor_col: int = 0

    @property
    def current_line(self) -> str:
        if 0 <= self.cursor_row < len(self.lines):
            return self.lines[self.cursor_row]
        return ""

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def is_empty(self) -> bool:
        return len(self.lines) == 1 and self.lines[0] == ""

    def get_text(self) -> str:
        return "\n".join(self.lines)

    def set_text(self, text: str) -> None:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = normalized.split("\n") if normalized else [""]
        self.cursor_row = len(self.lines) - 1
        self.cursor_col = len(self.lines[self.cursor_row])

    def insert_char(self, char: str) -> None:
        if char in ("\r", "\n"):
            self.split_line()
            return

        line = self.lines[self.cursor_row]
        self.lines[self.cursor_row] = (
            line[: self.cursor_col] + char + line[self.cursor_col :]
        )
        self.cursor_col += len(char)

    def insert_text(self, text: str) -> None:
        """Insert arbitrary single or multiline string at current cursor position."""
        if not text:
            return

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        insert_lines = normalized.split("\n")

        if len(insert_lines) == 1:
            line = self.lines[self.cursor_row]
            self.lines[self.cursor_row] = (
                line[: self.cursor_col] + insert_lines[0] + line[self.cursor_col :]
            )
            self.cursor_col += len(insert_lines[0])
            return

        current_line = self.lines[self.cursor_row]
        prefix = current_line[: self.cursor_col]
        suffix = current_line[self.cursor_col :]

        first_line = prefix + insert_lines[0]
        middle_lines = insert_lines[1:-1]
        last_line = insert_lines[-1] + suffix

        new_lines = (
            self.lines[: self.cursor_row]
            + [first_line]
            + middle_lines
            + [last_line]
            + self.lines[self.cursor_row + 1 :]
        )
        self.lines = new_lines
        self.cursor_row = self.cursor_row + len(insert_lines) - 1
        self.cursor_col = len(insert_lines[-1])

    def split_line(self) -> None:
        """Split current line at cursor position (Newline/Enter)."""
        line = self.lines[self.cursor_row]
        first_half = line[: self.cursor_col]
        second_half = line[self.cursor_col :]

        self.lines[self.cursor_row] = first_half
        self.lines.insert(self.cursor_row + 1, second_half)
        self.cursor_row += 1
        self.cursor_col = 0

    def delete_backspace(self) -> bool:
        """Delete character before cursor. Returns True if modified."""
        if self.cursor_col > 0:
            line = self.lines[self.cursor_row]
            self.lines[self.cursor_row] = (
                line[: self.cursor_col - 1] + line[self.cursor_col :]
            )
            self.cursor_col -= 1
            return True

        if self.cursor_row > 0:
            prev_line = self.lines[self.cursor_row - 1]
            curr_line = self.lines[self.cursor_row]
            self.cursor_col = len(prev_line)
            self.lines[self.cursor_row - 1] = prev_line + curr_line
            del self.lines[self.cursor_row]
            self.cursor_row -= 1
            return True

        return False

    def delete_forward(self) -> bool:
        """Delete character at cursor (Delete key). Returns True if modified."""
        line = self.lines[self.cursor_row]
        if self.cursor_col < len(line):
            self.lines[self.cursor_row] = (
                line[: self.cursor_col] + line[self.cursor_col + 1 :]
            )
            return True

        if self.cursor_row < len(self.lines) - 1:
            next_line = self.lines[self.cursor_row + 1]
            self.lines[self.cursor_row] = line + next_line
            del self.lines[self.cursor_row + 1]
            return True

        return False

    def move_cursor_left(self) -> None:
        if self.cursor_col > 0:
            self.cursor_col -= 1
        elif self.cursor_row > 0:
            self.cursor_row -= 1
            self.cursor_col = len(self.lines[self.cursor_row])

    def move_cursor_right(self) -> None:
        line_len = len(self.lines[self.cursor_row])
        if self.cursor_col < line_len:
            self.cursor_col += 1
        elif self.cursor_row < len(self.lines) - 1:
            self.cursor_row += 1
            self.cursor_col = 0

    def move_cursor_up(self) -> None:
        if self.cursor_row > 0:
            self.cursor_row -= 1
            self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))

    def move_cursor_down(self) -> None:
        if self.cursor_row < len(self.lines) - 1:
            self.cursor_row += 1
            self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))

    def move_to_line_start(self) -> None:
        self.cursor_col = 0

    def move_to_line_end(self) -> None:
        self.cursor_col = len(self.lines[self.cursor_row])

    def move_to_buffer_start(self) -> None:
        self.cursor_row = 0
        self.cursor_col = 0

    def move_to_buffer_end(self) -> None:
        self.cursor_row = len(self.lines) - 1
        self.cursor_col = len(self.lines[self.cursor_row])

    def clear(self) -> None:
        self.lines = [""]
        self.cursor_row = 0
        self.cursor_col = 0

    def clear_line(self) -> None:
        self.lines[self.cursor_row] = ""
        self.cursor_col = 0

    def clear_to_end_of_line(self) -> None:
        self.lines[self.cursor_row] = self.lines[self.cursor_row][: self.cursor_col]


class InteractivePromptReader:
    """Rich interactive multiline prompt reader with Ctrl+V clipboard and burst paste support."""

    def __init__(
        self,
        prompt_prefix: str = "claude> ",
        continuation_prefix: str = "...     ",
    ) -> None:
        self.prompt_prefix = prompt_prefix
        self.continuation_prefix = continuation_prefix
        self.buffer = MultilineBuffer()
        self._rendered_line_count = 1

    def read_prompt(self, initial_text: str | None = None) -> str | None:
        """Read a complete prompt interactively from terminal.

        Returns:
            The complete prompt text, or None if cancelled (Ctrl+C on empty buffer or /exit).
        """
        configure_windows_console_modes()

        if initial_text:
            self.buffer.set_text(initial_text)

        if not sys.stdin.isatty():
            try:
                return sys.stdin.read().strip()
            except Exception:
                return None

        if sys.platform == "win32":
            return self._read_windows_loop()
        else:
            return self._read_posix_fallback()

    def _render(self) -> None:
        """Render prompt buffer cleanly to terminal."""
        if self._rendered_line_count > 1:
            sys.stdout.write(f"\x1b[{self._rendered_line_count - 1}A\r\x1b[0J")
        else:
            sys.stdout.write("\r\x1b[0J")

        lines = self.buffer.lines
        for idx, line in enumerate(lines):
            prefix = self.prompt_prefix if idx == 0 else self.continuation_prefix
            sys.stdout.write(f"{prefix}{line}\n")

        self._rendered_line_count = len(lines)

        lines_up = len(lines) - 1 - self.buffer.cursor_row
        prefix_len = (
            len(self.prompt_prefix)
            if self.buffer.cursor_row == 0
            else len(self.continuation_prefix)
        )
        col_pos = prefix_len + self.buffer.cursor_col + 1

        if lines_up > 0:
            sys.stdout.write(f"\x1b[{lines_up}A\r\x1b[{col_pos}G")
        else:
            sys.stdout.write(f"\r\x1b[{col_pos}G")
        sys.stdout.flush()

    def _read_windows_loop(self) -> str | None:
        import msvcrt

        self._render()

        while True:
            ch = msvcrt.get_wch()

            if ch == "\x16":
                clip = get_clipboard_text()
                if clip:
                    self.buffer.insert_text(clip)
                    self._render()
                continue

            if ch in ("\x00", "\xe0"):
                code = msvcrt.get_wch()
                if code == "H":
                    self.buffer.move_cursor_up()
                elif code == "P":
                    self.buffer.move_cursor_down()
                elif code == "K":
                    self.buffer.move_cursor_left()
                elif code == "M":
                    self.buffer.move_cursor_right()
                elif code == "G":
                    self.buffer.move_to_line_start()
                elif code == "O":
                    self.buffer.move_to_line_end()
                elif code == "S":
                    self.buffer.delete_forward()
                elif code == "w":
                    self.buffer.move_to_buffer_start()
                elif code == "u":
                    self.buffer.move_to_buffer_end()
                self._render()
                continue

            if ch in ("\r", "\n"):
                if msvcrt.kbhit():
                    self.buffer.split_line()
                    self._render()
                    continue

                text = self.buffer.get_text().strip()
                if text in ("/paste", "/p"):
                    self.buffer.clear()
                    clip = get_clipboard_text()
                    if clip:
                        self.buffer.insert_text(clip)
                    self._render()
                    continue
                elif text in ("/clear", "/c"):
                    self.buffer.clear()
                    self._render()
                    continue
                elif text in ("/exit", "/quit", "/q"):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return None
                elif text in ("/help", "/?"):
                    sys.stdout.write("\n")
                    print("  [Interactive Prompt Commands & Shortcuts]")
                    print("  Ctrl+V or /paste  : Paste clipboard contents")
                    print("  Enter             : Submit prompt")
                    print("  /clear            : Clear buffer")
                    print("  /exit             : Exit without submitting")
                    print("  Arrow keys        : Navigate text and lines")
                    self.buffer.clear()
                    self._render()
                    continue

                sys.stdout.write("\n")
                sys.stdout.flush()
                return self.buffer.get_text()

            if ch == "\x08":
                self.buffer.delete_backspace()
                self._render()
                continue

            if ch == "\x01":
                self.buffer.move_to_line_start()
                self._render()
                continue

            if ch == "\x05":
                self.buffer.move_to_line_end()
                self._render()
                continue

            if ch == "\x0b":
                self.buffer.clear_to_end_of_line()
                self._render()
                continue

            if ch == "\x15":
                self.buffer.clear_line()
                self._render()
                continue

            if ch == "\x03":
                if not self.buffer.is_empty:
                    self.buffer.clear()
                    self._render()
                else:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return None
                continue

            if ch == "\x04":
                if not self.buffer.is_empty:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return self.buffer.get_text()
                else:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return None

            if ch >= " ":
                burst = [ch]
                while msvcrt.kbhit():
                    next_ch = msvcrt.get_wch()
                    if next_ch in ("\x00", "\xe0"):
                        msvcrt.get_wch()
                        continue
                    if next_ch in ("\r", "\n"):
                        burst.append("\n")
                    else:
                        burst.append(next_ch)
                self.buffer.insert_text("".join(burst))
                self._render()

    def _read_posix_fallback(self) -> str | None:
        try:
            return input(self.prompt_prefix)
        except (EOFError, KeyboardInterrupt):
            return None


def diagnose_terminal_input() -> dict[str, object]:
    """Inspect and report terminal, console modes, streams, and clipboard diagnostics."""
    results: dict[str, object] = {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "python_version": platform.python_version(),
            "is_windows": sys.platform == "win32",
        },
        "streams": {
            "stdin_isatty": (
                sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False
            ),
            "stdout_isatty": (
                sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False
            ),
            "stderr_isatty": (
                sys.stderr.isatty() if hasattr(sys.stderr, "isatty") else False
            ),
            "stdin_encoding": getattr(sys.stdin, "encoding", None),
            "stdout_encoding": getattr(sys.stdout, "encoding", None),
            "stderr_encoding": getattr(sys.stderr, "encoding", None),
        },
        "environment": {
            "TERM": os.environ.get("TERM"),
            "WT_SESSION": os.environ.get("WT_SESSION") is not None,
            "ConEmuPID": os.environ.get("ConEmuPID"),
            "PYTHONUTF8": os.environ.get("PYTHONUTF8"),
            "PYTHONIOENCODING": os.environ.get("PYTHONIOENCODING"),
            "SHELL": os.environ.get("SHELL"),
            "ComSpec": os.environ.get("ComSpec"),
        },
    }

    if sys.platform == "win32":
        console_modes = configure_windows_console_modes()
        results["console_modes"] = console_modes

    clip_text = get_clipboard_text()
    if clip_text is not None:
        preview = (
            f"{clip_text[:30]}... ({len(clip_text)} chars total)"
            if len(clip_text) > 30
            else clip_text
        )
        results["clipboard"] = {
            "available": True,
            "char_count": len(clip_text),
            "line_count": len(clip_text.splitlines()),
            "has_multiline": "\n" in clip_text,
            "sample_safe_preview": preview,
        }
    else:
        results["clipboard"] = {
            "available": False,
            "char_count": 0,
            "line_count": 0,
            "has_multiline": False,
            "sample_safe_preview": None,
        }

    return results


def print_diagnostics(
    diag: dict[str, object], file: TextIO | None = None
) -> None:
    """Print human-readable diagnostic report to output stream."""
    out = file or sys.stdout
    print("=" * 64, file=out)
    print("        FREE CLAUDE CODE - TERMINAL & INPUT DIAGNOSTICS", file=out)
    print("=" * 64, file=out)

    plat = diag.get("platform", {})
    if isinstance(plat, dict):
        print("\n[Platform & Environment]", file=out)
        print(
            f"  OS:             {plat.get('system')} {plat.get('release')} ({plat.get('version')})",
            file=out,
        )
        print(f"  Python:         {plat.get('python_version')}", file=out)

    env = diag.get("environment", {})
    if isinstance(env, dict):
        print(f"  TERM:           {env.get('TERM', 'N/A')}", file=out)
        print(
            f"  Windows Term:   {'Active' if env.get('WT_SESSION') else 'Not detected'}",
            file=out,
        )
        print(f"  PYTHONUTF8:     {env.get('PYTHONUTF8', 'N/A')}", file=out)

    streams = diag.get("streams", {})
    if isinstance(streams, dict):
        print("\n[Standard Streams & Encoding]", file=out)
        print(
            f"  stdin:          isatty={streams.get('stdin_isatty')}, encoding={streams.get('stdin_encoding')}",
            file=out,
        )
        print(
            f"  stdout:         isatty={streams.get('stdout_isatty')}, encoding={streams.get('stdout_encoding')}",
            file=out,
        )
        print(
            f"  stderr:         isatty={streams.get('stderr_isatty')}, encoding={streams.get('stderr_encoding')}",
            file=out,
        )

    if "console_modes" in diag and isinstance(diag["console_modes"], dict):
        cmodes = diag["console_modes"]
        print("\n[Windows Console Modes]", file=out)
        print(f"  Configured:     {cmodes.get('configured')}", file=out)
        print(
            f"  VT Input:       {'Enabled (0x0200)' if cmodes.get('vt_input_enabled') else 'Disabled'}",
            file=out,
        )
        print(
            f"  VT Output:      {'Enabled (0x0004)' if cmodes.get('vt_output_enabled') else 'Disabled'}",
            file=out,
        )
        print(
            f"  QuickEdit Mode: {'Enabled (0x0040)' if cmodes.get('quick_edit_enabled') else 'Disabled'}",
            file=out,
        )
        print(f"  Code Page:      {cmodes.get('codepage', 'N/A')}", file=out)

    clip = diag.get("clipboard", {})
    if isinstance(clip, dict):
        print("\n[Clipboard Status]", file=out)
        print(f"  Accessible:     {clip.get('available')}", file=out)
        print(f"  Characters:     {clip.get('char_count')}", file=out)
        print(f"  Lines:          {clip.get('line_count')}", file=out)
        print(f"  Multiline:      {clip.get('has_multiline')}", file=out)

    print("\n" + "=" * 64, file=out)
    print("Diagnostic check completed successfully.", file=out)
    print("=" * 64 + "\n", file=out)


def inject_text_into_console(text: str, h_stdin: object = None) -> int:
    """Inject a string as Unicode KEY_EVENT_RECORDs into the console input buffer."""
    if sys.platform != "win32" or not text:
        return 0

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        _setup_win32_signatures(user32, kernel32)
    except Exception:
        return 0

    if h_stdin is None:
        h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    records = []
    for ch in normalized:
        rec_down = INPUT_RECORD()
        rec_down.EventType = 1  # KEY_EVENT
        rec_down.Event.KeyEvent.bKeyDown = True
        rec_down.Event.KeyEvent.wRepeatCount = 1
        rec_down.Event.KeyEvent.wVirtualKeyCode = 0
        rec_down.Event.KeyEvent.wVirtualScanCode = 0
        rec_down.Event.KeyEvent.uChar.UnicodeChar = ch
        rec_down.Event.KeyEvent.dwControlKeyState = 0
        records.append(rec_down)

        rec_up = INPUT_RECORD()
        rec_up.EventType = 1  # KEY_EVENT
        rec_up.Event.KeyEvent.bKeyDown = False
        rec_up.Event.KeyEvent.wRepeatCount = 1
        rec_up.Event.KeyEvent.wVirtualKeyCode = 0
        rec_up.Event.KeyEvent.wVirtualScanCode = 0
        rec_up.Event.KeyEvent.uChar.UnicodeChar = ch
        rec_up.Event.KeyEvent.dwControlKeyState = 0
        records.append(rec_up)

    if not records:
        return 0

    array_type = INPUT_RECORD * len(records)
    record_array = array_type(*records)
    import ctypes.wintypes

    written = ctypes.wintypes.DWORD()
    ok = kernel32.WriteConsoleInputW(
        h_stdin,
        ctypes.byref(record_array),
        len(records),
        ctypes.byref(written),
    )
    return written.value if ok else 0


class WindowsConsolePasteBridge:
    """Active Windows console paste interceptor and bridge.

    Runs during interactive client CLI sessions (e.g. Claude Code, Codex).
    When the user focuses the console window and presses Ctrl+V (or Shift+Insert),
    the bridge intercepts the raw keystroke, extracts full multiline unicode text
    from the Windows clipboard via Win32 API, and injects the character stream
    directly into the console input buffer using WriteConsoleInputW.
    Also ensures QuickEdit mode remains active for native right-click paste.
    """

    def __init__(self) -> None:
        import threading

        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._hook_handle: int | None = None
        self._running = False
        self._hook_proc_ref: object = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        """Start the background paste bridge hook."""
        if sys.platform != "win32":
            return False
        if self._running:
            return True

        import threading

        self._running = True
        started_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run_bridge,
            args=(started_event,),
            name="fcc-windows-paste-bridge",
            daemon=True,
        )
        self._thread.start()
        started_event.wait(timeout=1.0)
        return True

    def stop(self) -> None:
        """Stop the background paste bridge hook cleanly."""
        if not self._running:
            return
        self._running = False
        if sys.platform == "win32" and self._thread_id:
            try:
                user32 = ctypes.windll.user32
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        self._thread_id = 0

    def _run_bridge(self, started_event: object) -> None:
        import ctypes.wintypes

        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            _setup_win32_signatures(user32, kernel32)

            self._thread_id = kernel32.GetCurrentThreadId()
            h_console = kernel32.GetConsoleWindow()
            h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)

            def _hook_callback(
                nCode: int, wParam: int, lParam: ctypes.POINTER(KBDLLHOOKSTRUCT)
            ) -> int:
                if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    try:
                        h_fg = user32.GetForegroundWindow()
                        if h_console == 0 or h_fg == h_console:
                            vk = lParam.contents.vkCode
                            ctrl_down = bool(
                                (user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                            )
                            shift_down = bool(
                                (user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                            )
                            if (vk == VK_V and ctrl_down) or (
                                vk == VK_INSERT and shift_down
                            ):
                                clip = get_windows_clipboard_text()
                                if clip:
                                    inject_text_into_console(clip, h_stdin)
                                    return 1  # Intercept and suppress raw key event
                    except Exception:
                        pass
                return user32.CallNextHookEx(self._hook_handle, nCode, wParam, lParam)

            self._hook_proc_ref = HOOKPROC(_hook_callback)
            h_mod = kernel32.GetModuleHandleW(None)
            self._hook_handle = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                self._hook_proc_ref,
                h_mod,
                0,
            )

            if hasattr(started_event, "set"):
                started_event.set()

            # Windows message pump
            msg = ctypes.wintypes.MSG()
            while self._running:
                try:
                    mode = ctypes.c_uint32()
                    if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
                        if not (mode.value & ENABLE_QUICK_EDIT_MODE):
                            kernel32.SetConsoleMode(
                                h_stdin,
                                mode.value
                                | ENABLE_QUICK_EDIT_MODE
                                | ENABLE_EXTENDED_FLAGS,
                            )
                except Exception:
                    pass

                user32.MsgWaitForMultipleObjects(0, None, False, 100, 0x04FF)
                while user32.PeekMessageW(
                    ctypes.byref(msg), None, 0, 0, 1  # PM_REMOVE
                ):
                    if msg.message == WM_QUIT:
                        return
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))

        except Exception:
            pass
        finally:
            if hasattr(started_event, "set"):
                started_event.set()
            if self._hook_handle:
                try:
                    user32 = ctypes.windll.user32
                    user32.UnhookWindowsHookEx(self._hook_handle)
                except Exception:
                    pass
                self._hook_handle = None

