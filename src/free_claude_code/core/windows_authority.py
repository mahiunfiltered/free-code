from __future__ import annotations

"""Windows Execution Authority & Permission Diagnostic Profile.

Provides the runtime with full legitimate execution capabilities on Windows:
- Administrator status detection
- Process integrity level detection
- PowerShell execution policy inspection
- Filesystem write validation
- Actionable elevation diagnostics without security bypass
"""

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from free_claude_code.core.shell_resolver import get_shell_report, resolve_shell


@dataclass
class WindowsAuthorityProfile:
    user: str
    is_admin: bool
    integrity_level: str
    powershell_shell: str
    powershell_executable: str
    execution_policy: str
    cmd_available: bool
    git_available: bool
    wsl_available: bool
    git_bash_available: bool
    network_available: bool
    workspace_writable: bool
    workspace_path: str
    elevation_hint: str = ""


class WindowsAuthorityManager:
    """Inspects and manages Windows execution authority and permissions."""

    def __init__(self, workspace_path: str = "D:\\Claude code") -> None:
        self.workspace_path = os.path.abspath(workspace_path)

    def _is_admin(self) -> bool:
        if sys.platform != "win32":
            return os.geteuid() == 0 if hasattr(os, "geteuid") else False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin() != 0)
        except Exception:
            return False

    def _get_execution_policy(self) -> str:
        if sys.platform != "win32":
            return "N/A"
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-ExecutionPolicy"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return proc.stdout.strip() or "Unknown"
        except Exception:
            return "Restricted"

    def _test_workspace_writable(self) -> bool:
        test_file = os.path.join(self.workspace_path, f".perm_test_{os.getpid()}.tmp")
        try:
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("OK")
            os.remove(test_file)
            return True
        except Exception:
            return False

    def inspect_authority(self) -> WindowsAuthorityProfile:
        """Inspects and builds a complete Windows Authority diagnostic profile."""
        user = os.environ.get("USERNAME", os.environ.get("USER", "unknown"))
        is_admin = self._is_admin()
        integrity = "High (Administrator)" if is_admin else "Medium (Standard User)"

        shell_rep = get_shell_report()
        resolved_ps = resolve_shell(preferred="pwsh")

        exec_policy = self._get_execution_policy()
        cmd_ok = bool(shell_rep.get("cmd"))
        git_ok = bool(shutil.which("git"))
        wsl_ok = bool(shell_rep.get("wsl"))
        git_bash_ok = bool(shell_rep.get("git_bash"))
        writable = self._test_workspace_writable()

        elevation_hint = ""
        if not is_admin:
            elevation_hint = "Run powershell as Administrator: Start-Process powershell -Verb RunAs"

        return WindowsAuthorityProfile(
            user=user,
            is_admin=is_admin,
            integrity_level=integrity,
            powershell_shell=resolved_ps.shell_type.value,
            powershell_executable=resolved_ps.executable,
            execution_policy=exec_policy,
            cmd_available=cmd_ok,
            git_available=git_ok,
            wsl_available=wsl_ok,
            git_bash_available=git_bash_ok,
            network_available=True,
            workspace_writable=writable,
            workspace_path=self.workspace_path,
            elevation_hint=elevation_hint,
        )

    def print_authority_banner(self, profile: WindowsAuthorityProfile | None = None) -> None:
        """Prints the Windows Authority diagnostic banner."""
        prof = profile or self.inspect_authority()
        admin_str = "YES (Elevated)" if prof.is_admin else "NO (Standard User)"
        write_str = "YES (Full Access)" if prof.workspace_writable else "NO (Access Denied)"

        print("\n" + "=" * 65)
        print(" WINDOWS EXECUTION AUTHORITY — AUTONOMOUS_WINDOWS PROFILE")
        print("=" * 65)
        print(f" USER           : {prof.user}")
        print(f" ADMIN          : {admin_str}")
        print(f" INTEGRITY      : {prof.integrity_level}")
        print(f" POWERSHELL     : {prof.powershell_shell} ({prof.powershell_executable})")
        print(f" EXEC POLICY    : {prof.execution_policy}")
        print(f" CMD            : {'YES' if prof.cmd_available else 'NO'}")
        print(f" GIT            : {'YES' if prof.git_available else 'NO'}")
        print(f" GIT BASH       : {'YES' if prof.git_bash_available else 'NO'}")
        print(f" WSL            : {'YES' if prof.wsl_available else 'NO'}")
        print(f" WRITE ACCESS   : {write_str} -> {prof.workspace_path}")
        if prof.elevation_hint:
            print(f" ELEVATION HINT : {prof.elevation_hint}")
        print("=" * 65 + "\n")


    def check_permission(self, operation: str) -> tuple[bool, str]:
        """Checks if the operation is permitted under current Windows execution authority."""
        op_lower = operation.lower()
        admin_required_ops = (
            "sc create", "sc config", "sc delete", "netsh advfirewall", "diskpart",
            "format ", "set-executionpolicy unrestricted", "takeown", "icacls /grant:r administrators",
            "install-windowsfeature", "dism /online"
        )
        is_admin_req = any(kw in op_lower for kw in admin_required_ops)
        
        if is_admin_req and not self._is_admin():
            return False, f"Administrator privileges required for: '{operation}'. Launch with elevation: Start-Process powershell -Verb RunAs"
        
        return True, "Permitted under current Windows authority"

    def request_elevated_helper(self, command: str, wait: bool = True) -> subprocess.CompletedProcess[str]:
        """Launches a Windows UAC elevated helper process via Start-Process powershell -Verb RunAs."""
        if sys.platform != "win32":
            return subprocess.run(["sudo", "sh", "-c", command], capture_output=True, text=True)
        
        escaped_cmd = command.replace('"', '`"')
        ps_cmd = f"Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile', '-Command', '{escaped_cmd}'"
        if wait:
            ps_cmd += " -Wait"
        
        return subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=60,
        )


_GLOBAL_WINDOWS_AUTHORITY = WindowsAuthorityManager()


def get_windows_authority() -> WindowsAuthorityManager:
    return _GLOBAL_WINDOWS_AUTHORITY
