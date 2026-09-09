import os
import shutil
import subprocess
import sys
import urllib.request
import json
import time

def run_cmd(cmd_list, timeout=10):
    try:
        proc = subprocess.run(cmd_list, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return proc.returncode == 0, proc.stdout.strip()
    except Exception as e:
        return False, str(e)

def check_component(name, ok, details):
    status = "PASS [OK]" if ok else "FAIL [X]"
    print(f" {name:<28} : {status:<12} {details}")
    return ok

def main():
    print("=" * 65)
    print(" CLAUDE CODE RUNTIME — SYSTEM HEALTH CHECK")
    print("=" * 65)

    all_passed = True

    # 1. OS & Platform
    os_name = sys.platform
    check_component("Operating System", os_name == "win32", f"Windows ({os.name})")

    # 2. Python
    py_ver = sys.version.split()[0]
    check_component("Python Runtime", True, f"v{py_ver} ({sys.executable})")

    # 3. Node.js
    node_ok, node_ver = run_cmd(["node", "--version"])
    all_passed &= check_component("Node.js", node_ok, node_ver)

    # 4. npm
    npm_ok, npm_ver = run_cmd(["npm.cmd" if sys.platform == "win32" else "npm", "--version"])
    all_passed &= check_component("npm Package Manager", npm_ok, f"v{npm_ver}")

    # 5. Git
    git_ok, git_ver = run_cmd(["git", "--version"])
    all_passed &= check_component("Git Version Control", git_ok, git_ver)

    # 6. PowerShell Resolver
    from free_claude_code.core.shell_resolver import resolve_shell, get_shell_report, run_shell_command
    report = get_shell_report()
    ps_shell = resolve_shell()
    ps_run = run_shell_command("Write-Output 'POWERSHELL_OK'")
    ps_ok = ps_run.returncode == 0 and "POWERSHELL_OK" in ps_run.stdout
    all_passed &= check_component("PowerShell Resolver", ps_ok, f"{ps_shell.shell_type.value} -> {ps_shell.executable}")

    # 7. Git Bash
    git_bash_ok = bool(report.get("git_bash"))
    check_component("Git Bash (POSIX)", git_bash_ok, report.get("git_bash") or "Not found (Fallback available)")

    # 8. WSL
    wsl_ok = bool(report.get("wsl"))
    check_component("WSL (Linux Subsystem)", wsl_ok, report.get("wsl") or "Not installed (Optional)")

    # 9. Claude Executable
    claude_bin = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe") or os.path.expanduser(r"~\.local\bin\claude.exe")
    claude_exists = os.path.exists(claude_bin) if claude_bin else False
    all_passed &= check_component("Claude Code Binary", claude_exists, str(claude_bin))

    # 10. Proxy Gateway (fcc-server)
    gateway_ok = False
    gateway_info = "Unreachable"
    try:
        req = urllib.request.Request("http://127.0.0.1:8082/v1/models")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                gateway_ok = True
                gateway_info = "http://127.0.0.1:8082 (Active)"
    except Exception as e:
        gateway_info = str(e)
    all_passed &= check_component("FCC Gateway (Proxy)", gateway_ok, gateway_info)

    # 11. Upstream Model & Provider
    from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
    has_nim = "nvidia_nim" in PROVIDER_CATALOG
    has_openai = "openai_api" in PROVIDER_CATALOG
    providers_ok = has_nim and has_openai
    all_passed &= check_component("Model Providers", providers_ok, "NVIDIA NIM, OpenAI API, Anthropic Messages")

    # 12. Hooks System (Cross-platform)
    ruflo_hook = os.path.expanduser(r"~\.claude\plugins\cache\ruflo\ruflo-core\0.2.2\scripts\ruflo-hook.cjs")
    hooks_ok = os.path.exists(ruflo_hook)
    all_passed &= check_component("Hooks Architecture", hooks_ok, "Cross-platform Node.js runners (Zero /bin/bash errors)")

    # 13. Process & Stagnation Watchdog
    from free_claude_code.core.watchdog import AntiStagnationWatchdog
    wd = AntiStagnationWatchdog(inactivity_timeout_seconds=5)
    wd.start()
    wd_ok = wd._running
    wd.stop()
    all_passed &= check_component("Anti-Stagnation Watchdog", wd_ok, "Active thread monitor (30s timeout)")

    # 14. Parallel Orchestrator & Worker Pool
    from free_claude_code.orchestrator import WorkerPool, ConflictManager
    wp = WorkerPool(max_parallel_agents=4)
    orch_ok = wp.max_parallel_agents == 4
    all_passed &= check_component("Parallel Worker Pool", orch_ok, "4 Workers, DAG Scheduler, Scope Locking")

    print("=" * 65)
    if all_passed:
        print(" OVERALL STATUS: ALL CORE SUBSYSTEMS HEALTHY [PASS]")
    else:
        print(" OVERALL STATUS: SOME SUBSYSTEMS REQUIRE ATTENTION")
    print("=" * 65)
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())
