from __future__ import annotations

"""Complete system environment diagnostics tool for Free Claude Code on Windows.

Inspects hardware, OS, runtime versions, shell availability, developer tools,
network connectivity, provider configuration, and safe latency diagnostics.
Never prints or leaks secrets/API keys.
"""

import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Any

from free_claude_code.core.shell_resolver import get_shell_report, resolve_shell
from free_claude_code.core.windows_authority import get_windows_authority
from free_claude_code.runtime.service_manager import get_service_manager


def check_tool_version(cmd_list: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd_list, capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            return proc.stdout.strip().split("\n")[0]
        return None
    except Exception:
        return None


def measure_provider_latency(url: str = "http://127.0.0.1:8082/v1/models") -> tuple[bool, float, str]:
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            dur = time.time() - t0
            return (resp.status == 200, dur, f"HTTP {resp.status}")
    except Exception as e:
        return (False, time.time() - t0, str(e))


def run_diagnostics() -> dict[str, Any]:
    # 1. OS & Hardware
    os_name = platform.system()
    os_ver = platform.version()
    os_release = platform.release()
    cpu_count = os.cpu_count() or 1
    arch = platform.machine()

    # 2. Windows Authority & Permissions
    auth_mgr = get_windows_authority()
    auth_prof = auth_mgr.inspect_authority()

    # 3. Development Binaries
    py_ver = sys.version.split()[0]
    node_ver = check_tool_version(["node", "--version"])
    npm_ver = check_tool_version(["npm.cmd" if sys.platform == "win32" else "npm", "--version"])
    git_ver = check_tool_version(["git", "--version"])
    uv_ver = check_tool_version(["uv", "--version"])
    docker_ver = check_tool_version(["docker", "--version"])

    # 4. Shell Discovery
    shell_rep = get_shell_report()
    default_shell = resolve_shell()

    # 5. Gateway & Provider Health
    prov_ok, prov_latency, prov_details = measure_provider_latency()

    # Build report
    report = {
        "timestamp": time.time(),
        "environment": {
            "os": f"{os_name} {os_release} (Build {os_ver})",
            "architecture": arch,
            "cpu_cores": cpu_count,
            "python_executable": sys.executable,
            "python_version": py_ver,
        },
        "windows_authority": {
            "user": auth_prof.user,
            "is_admin": auth_prof.is_admin,
            "integrity_level": auth_prof.integrity_level,
            "execution_policy": auth_prof.execution_policy,
            "workspace_writable": auth_prof.workspace_writable,
            "workspace_path": auth_prof.workspace_path,
            "elevation_hint": auth_prof.elevation_hint,
        },
        "toolchains": {
            "node": node_ver or "Not found",
            "npm": npm_ver or "Not found",
            "git": git_ver or "Not found",
            "uv": uv_ver or "Not found",
            "docker": docker_ver or "Not found",
        },
        "shells": {
            "default_resolved": f"{default_shell.shell_type.value} -> {default_shell.executable}",
            "powershell": shell_rep.get("powershell"),
            "pwsh": shell_rep.get("pwsh"),
            "cmd": shell_rep.get("cmd"),
            "git_bash": shell_rep.get("git_bash"),
            "wsl": shell_rep.get("wsl"),
        },
        "gateway_and_models": {
            "endpoint": "http://127.0.0.1:8082",
            "is_healthy": prov_ok,
            "latency_seconds": round(prov_latency, 3),
            "details": prov_details,
            "model_configured": "nvidia_nim/nvidia/nemotron-3-super-120b-a12b",
        },
    }

    return report


def main():
    print("=" * 70)
    print(" FREE CLAUDE CODE — COMPLETE SYSTEM ENVIRONMENT DIAGNOSTICS")
    print("=" * 70)

    rep = run_diagnostics()

    env = rep["environment"]
    print(f" [OS & Hardware]")
    print(f"   OS             : {env['os']} ({env['architecture']})")
    print(f"   CPU Cores      : {env['cpu_cores']}")
    print(f"   Python         : v{env['python_version']} ({env['python_executable']})")

    auth = rep["windows_authority"]
    print(f"\n [Windows Execution Authority]")
    print(f"   User           : {auth['user']}")
    print(f"   Administrator  : {'YES (Elevated)' if auth['is_admin'] else 'NO (Standard User)'}")
    print(f"   Integrity      : {auth['integrity_level']}")
    print(f"   Exec Policy    : {auth['execution_policy']}")
    print(f"   Workspace      : {auth['workspace_path']} (Writable: {auth['workspace_writable']})")

    tools = rep["toolchains"]
    print(f"\n [Installed Toolchains]")
    print(f"   Git            : {tools['git']}")
    print(f"   Node.js        : {tools['node']}")
    print(f"   npm            : {tools['npm']}")
    print(f"   uv             : {tools['uv']}")
    print(f"   Docker         : {tools['docker']}")

    shells = rep["shells"]
    print(f"\n [Shell Resolution Hierarchy]")
    print(f"   Default Native : {shells['default_resolved']}")
    print(f"   PowerShell 5.1 : {shells['powershell'] or 'Not found'}")
    print(f"   PowerShell 7   : {shells['pwsh'] or 'Not found'}")
    print(f"   CMD            : {shells['cmd'] or 'Not found'}")
    print(f"   Git Bash       : {shells['git_bash'] or 'Not found'}")
    print(f"   WSL            : {shells['wsl'] or 'Not found'}")

    gw = rep["gateway_and_models"]
    print(f"\n [Gateway & Model Intelligence]")
    print(f"   Endpoint       : {gw['endpoint']}")
    print(f"   Health Status  : {'ACTIVE [200 OK]' if gw['is_healthy'] else 'UNREACHABLE'}")
    print(f"   Latency        : {gw['latency_seconds']:.3f}s")
    print(f"   Model          : {gw['model_configured']}")

    print("=" * 70)
    print(" ALL ENVIRONMENT & TOOLCHAIN CHECKS COMPLETE")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
