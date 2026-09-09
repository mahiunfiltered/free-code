import os
import sys
import pytest
from free_claude_code.core.shell_resolver import (
    resolve_shell,
    get_shell_report,
    run_shell_command,
    ShellType,
    ResolvedShell,
)

def test_shell_discovery_report():
    report = get_shell_report()
    assert isinstance(report, dict)
    assert "pwsh" in report
    assert "powershell" in report
    assert "cmd" in report
    assert "git_bash" in report
    assert "wsl" in report

def test_default_shell_resolution():
    shell = resolve_shell()
    assert isinstance(shell, ResolvedShell)
    if sys.platform == "win32":
        assert shell.shell_type in (ShellType.PWSH, ShellType.POWERSHELL, ShellType.CMD)
        assert os.path.exists(shell.executable)
    else:
        assert shell.shell_type in (ShellType.POSIX_BASH, ShellType.POSIX_SH)

def test_powershell_execution():
    if sys.platform == "win32":
        res = run_shell_command("Write-Output 'POWERSHELL_SHELL_TEST_OK'")
        assert res.returncode == 0
        assert "POWERSHELL_SHELL_TEST_OK" in res.stdout

def test_cmd_execution():
    if sys.platform == "win32":
        res = run_shell_command("echo CMD_TEST_OK", preferred="cmd")
        assert res.returncode == 0
        assert "CMD_TEST_OK" in res.stdout

def test_bash_routing():
    if sys.platform == "win32":
        shell = resolve_shell(command="/bin/bash -c 'echo test'")
        # Should route to Git Bash or WSL rather than attempting /bin/bash literally
        assert shell.shell_type in (ShellType.GIT_BASH, ShellType.WSL, ShellType.PWSH, ShellType.POWERSHELL)
        assert not shell.executable.startswith("/bin/bash")
