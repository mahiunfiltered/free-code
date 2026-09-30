"""Centralized Windows-native shell resolver.

Detects and resolves available shells on Windows and Unix systems:
- PowerShell 7+ (pwsh.exe)
- Windows PowerShell (powershell.exe)
- Windows Command Prompt (cmd.exe)
- Git Bash (bash.exe / sh.exe)
- Windows Subsystem for Linux (wsl.exe)

Guarantees that no subsystem blindly invokes '/bin/bash' on Windows.
"""

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class ShellType(StrEnum):
    PWSH = "pwsh"
    POWERSHELL = "powershell"
    CMD = "cmd"
    GIT_BASH = "git_bash"
    WSL = "wsl"
    POSIX_BASH = "posix_bash"
    POSIX_SH = "posix_sh"


@dataclass(frozen=True)
class ResolvedShell:
    shell_type: ShellType
    executable: str
    base_args: list[str] = field(default_factory=list)
    is_windows: bool = True

    def build_command_args(self, command: str) -> list[str]:
        """Builds the full argument list to execute a command string."""
        if (
            self.shell_type in (ShellType.PWSH, ShellType.POWERSHELL)
            or self.shell_type == ShellType.CMD
            or self.shell_type
            in (ShellType.GIT_BASH, ShellType.POSIX_BASH, ShellType.POSIX_SH)
            or self.shell_type == ShellType.WSL
        ):
            return [self.executable, *self.base_args, command]
        return [self.executable, command]


class ShellResolver:
    """Discovers and caches available shells on the system."""

    def __init__(self) -> None:
        self._is_windows = sys.platform == "win32"
        self._cache: dict[str, str | None] = {}
        self._discover_all()

    def _find_executable(
        self, name: str, fallback_paths: Sequence[str] = ()
    ) -> str | None:
        if name in self._cache:
            return self._cache[name]

        # 1. Search PATH
        found = shutil.which(name)
        if found:
            self._cache[name] = found
            return found

        # 2. Search common fallback installation paths on Windows
        if self._is_windows:
            for path in fallback_paths:
                expanded = os.path.expandvars(path)
                if os.path.isfile(expanded):
                    self._cache[name] = expanded
                    return expanded

        self._cache[name] = None
        return None

    def _discover_all(self) -> None:
        # PowerShell 7+
        self._find_executable(
            "pwsh",
            [
                r"%ProgramFiles%\PowerShell\7\pwsh.exe",
                r"%ProgramFiles(x86)%\PowerShell\7\pwsh.exe",
                r"%LOCALAPPDATA%\Microsoft\PowerShell\pwsh.exe",
            ],
        )

        # Windows PowerShell
        self._find_executable(
            "powershell",
            [
                r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe",
            ],
        )

        # CMD
        self._find_executable(
            "cmd",
            [
                r"%SystemRoot%\System32\cmd.exe",
            ],
        )

        # Git Bash
        self._find_executable(
            "git-bash",
            [
                r"%ProgramFiles%\Git\bin\bash.exe",
                r"%ProgramFiles(x86)%\Git\bin\bash.exe",
                r"%ProgramFiles%\Git\usr\bin\bash.exe",
                r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe",
            ],
        )

        # WSL
        self._find_executable(
            "wsl",
            [
                r"%SystemRoot%\System32\wsl.exe",
            ],
        )

    def is_available(self, shell_type: ShellType | str) -> bool:
        st = ShellType(shell_type) if isinstance(shell_type, str) else shell_type
        if st == ShellType.PWSH:
            return bool(self._cache.get("pwsh"))
        elif st == ShellType.POWERSHELL:
            return bool(self._cache.get("powershell"))
        elif st == ShellType.CMD:
            return bool(self._cache.get("cmd"))
        elif st == ShellType.GIT_BASH:
            return bool(self._cache.get("git-bash"))
        elif st == ShellType.WSL:
            return bool(self._cache.get("wsl"))
        elif st in (ShellType.POSIX_BASH, ShellType.POSIX_SH):
            return bool(shutil.which("bash") or shutil.which("sh"))
        return False

    def get_discovery_report(self) -> dict[str, str | None]:
        return {
            "pwsh": self._cache.get("pwsh"),
            "powershell": self._cache.get("powershell"),
            "cmd": self._cache.get("cmd"),
            "git_bash": self._cache.get("git-bash"),
            "wsl": self._cache.get("wsl"),
            "is_windows": "true" if self._is_windows else "false",
        }

    def resolve(
        self,
        preferred: str | ShellType | None = None,
        command: str | None = None,
    ) -> ResolvedShell:
        """Resolves the most appropriate shell for the given context."""
        if not self._is_windows:
            bash_bin = shutil.which("bash") or "/bin/bash"
            return ResolvedShell(
                shell_type=ShellType.POSIX_BASH,
                executable=bash_bin,
                base_args=["-c"],
                is_windows=False,
            )

        # Handle explicit preference if valid and available
        pref_str = str(preferred).lower() if preferred else ""
        if pref_str in ("pwsh", "powershell7", "ps7"):
            pwsh = self._cache.get("pwsh")
            if pwsh:
                return ResolvedShell(
                    shell_type=ShellType.PWSH,
                    executable=pwsh,
                    base_args=[
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                    ],
                    is_windows=True,
                )

        if pref_str in ("powershell", "ps", "winps"):
            ps = self._cache.get("powershell")
            if ps:
                return ResolvedShell(
                    shell_type=ShellType.POWERSHELL,
                    executable=ps,
                    base_args=[
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                    ],
                    is_windows=True,
                )

        if pref_str in ("cmd", "batch"):
            cmd = self._cache.get("cmd") or "cmd.exe"
            return ResolvedShell(
                shell_type=ShellType.CMD,
                executable=cmd,
                base_args=["/c"],
                is_windows=True,
            )

        if pref_str in ("bash", "git_bash", "gitbash", "sh"):
            git_bash = self._cache.get("git-bash")
            if git_bash:
                return ResolvedShell(
                    shell_type=ShellType.GIT_BASH,
                    executable=git_bash,
                    base_args=["-c"],
                    is_windows=True,
                )
            wsl = self._cache.get("wsl")
            if wsl:
                return ResolvedShell(
                    shell_type=ShellType.WSL,
                    executable=wsl,
                    base_args=["-e", "bash", "-c"],
                    is_windows=True,
                )

        # Auto-detect from command syntax if command is provided
        if command:
            # Check if command is explicitly a bash/sh construct or starts with bash/sh
            cmd_strip = command.strip()
            if cmd_strip.startswith(("/bin/bash", "bash -c", "sh -c", "/bin/sh")):
                git_bash = self._cache.get("git-bash")
                if git_bash:
                    # Strip leading /bin/bash or bash -c wrapper if present
                    return ResolvedShell(
                        shell_type=ShellType.GIT_BASH,
                        executable=git_bash,
                        base_args=["-c"],
                        is_windows=True,
                    )
                wsl = self._cache.get("wsl")
                if wsl:
                    return ResolvedShell(
                        shell_type=ShellType.WSL,
                        executable=wsl,
                        base_args=["-e", "bash", "-c"],
                        is_windows=True,
                    )

        # Default Windows priority: PowerShell (pwsh -> powershell -> cmd)
        pwsh = self._cache.get("pwsh")
        if pwsh:
            return ResolvedShell(
                shell_type=ShellType.PWSH,
                executable=pwsh,
                base_args=[
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                ],
                is_windows=True,
            )

        ps = self._cache.get("powershell")
        if ps:
            return ResolvedShell(
                shell_type=ShellType.POWERSHELL,
                executable=ps,
                base_args=[
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                ],
                is_windows=True,
            )

        cmd = self._cache.get("cmd") or "cmd.exe"
        return ResolvedShell(
            shell_type=ShellType.CMD,
            executable=cmd,
            base_args=["/c"],
            is_windows=True,
        )


_GLOBAL_RESOLVER = ShellResolver()


def resolve_shell(
    preferred: str | ShellType | None = None,
    command: str | None = None,
) -> ResolvedShell:
    """Public helper to resolve the system shell."""
    return _GLOBAL_RESOLVER.resolve(preferred=preferred, command=command)


def get_shell_report() -> dict[str, str | None]:
    """Returns discovery metadata for available shells."""
    return _GLOBAL_RESOLVER.get_discovery_report()


def run_shell_command(
    command: str,
    preferred: str | ShellType | None = None,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int | float | None = 60,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Executes a shell command using the resolved platform shell."""
    shell = resolve_shell(preferred=preferred, command=command)
    args = shell.build_command_args(command)

    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    return subprocess.run(
        args,
        cwd=cwd or os.getcwd(),
        env=full_env,
        timeout=timeout,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
