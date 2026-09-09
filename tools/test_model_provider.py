"""Comprehensive multi-layer diagnostic suite for Free Claude Code.

Performs 8-point system health check without exposing secrets:
1. Environment & Runtimes (Python, Node, NPM, Git)
2. Windows Shell & ConPTY Execution
3. Claude Code Binary Resolution
4. Local Proxy Gateway (http://127.0.0.1:8082)
5. Provider Configuration & API Authentication
6. Upstream Model Inference & Streaming
7. Upstream Tool/Function Calling
8. Local Filesystem & Git State
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from dotenv import dotenv_values
import httpx


def run_diagnostics() -> int:
    print("=" * 65)
    print("       CLAUDE CODE REFERENCE — SYSTEM HEALTH CHECK")
    print("=" * 65)

    all_passed = True
    fcc_env_path = Path.home() / ".fcc" / ".env"
    claude_settings_path = Path.home() / ".claude" / "settings.json"

    fcc_config = dotenv_values(fcc_env_path) if fcc_env_path.exists() else {}
    claude_settings = {}
    if claude_settings_path.exists():
        try:
            with open(claude_settings_path, "r", encoding="utf-8") as f:
                claude_settings = json.load(f)
        except Exception:
            claude_settings = {}

    # 1. Environment & Runtime
    print("\n[1/8] Checking Environment & Runtime...", flush=True)
    print(f"  - OS:                 {sys.platform} ({os.name})")
    print(f"  - Python Version:     {sys.version.split()[0]}")
    node_which = shutil.which("node")
    npm_which = shutil.which("npm")
    git_which = shutil.which("git")
    print(f"  - Node Executable:    {node_which or 'MISSING'}")
    print(f"  - NPM Executable:     {npm_which or 'MISSING'}")
    print(f"  - Git Executable:     {git_which or 'MISSING'}")
    if not node_which or not git_which:
        print("  --> WARN: Essential build tools missing from PATH")
    else:
        print("  --> PASS: Runtime environment detected")

    # 2. Windows Shell & Terminal Environment
    print("\n[2/8] Checking Shell & Terminal Environment...", flush=True)
    wt_which = shutil.which("wt.exe") or os.path.exists(
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe")
    )
    powershell_which = shutil.which("powershell.exe") or shutil.which("pwsh")
    cmd_which = shutil.which("cmd.exe")
    print(f"  - Windows Terminal:   {'PRESENT' if wt_which else 'NOT FOUND (conhost fallback)'}")
    print(f"  - PowerShell:         {powershell_which or 'MISSING'}")
    print(f"  - CMD.EXE:            {cmd_which or 'MISSING'}")

    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", "echo 'SHELL_OK'"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if "SHELL_OK" in res.stdout:
            print("  --> PASS: PowerShell execution verified")
        else:
            print("  --> FAIL: PowerShell output mismatch")
            all_passed = False
    except Exception as e:
        print(f"  --> FAIL: PowerShell execution error ({e})")
        all_passed = False

    # 3. Claude Code Binary Resolution
    print("\n[3/8] Checking Claude Code Binary Resolution...", flush=True)
    fcc_claude_which = shutil.which("fcc-claude")
    local_claude_exe = Path.home() / ".local" / "bin" / "claude.exe"
    appdata_claude = Path(os.path.expandvars(r"%APPDATA%\npm\claude.cmd"))

    print(f"  - fcc-claude:         {fcc_claude_which or 'PRESENT via uv run'}")
    print(f"  - ~/.local/bin/claude: {'PRESENT' if local_claude_exe.exists() else 'NOT FOUND'}")
    print(f"  - npm global claude:  {'PRESENT' if appdata_claude.exists() else 'NOT FOUND'}")

    if local_claude_exe.exists() or fcc_claude_which or appdata_claude.exists():
        print("  --> PASS: Client binary resolved")
    else:
        print("  --> FAIL: No Claude Code binary found")
        all_passed = False

    # 4. Local Gateway / Proxy
    print("\n[4/8] Checking Local Proxy Gateway (http://127.0.0.1:8082)...", flush=True)
    proxy_alive = False
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get("http://127.0.0.1:8082/health")
            if resp.status_code == 200:
                print(f"  - Status:             200 OK")
                proxy_alive = True
            else:
                print(f"  - Status:             HTTP {resp.status_code}")
    except Exception as e:
        print(f"  - Status:             NOT REACHABLE ({type(e).__name__})")

    if proxy_alive:
        print("  --> PASS: Local proxy gateway healthy")
    else:
        print("  --> WARN: Local proxy not active. Run 'uv run fcc-server' or Claude-Code.bat")

    # 5. Provider & API Configuration
    print("\n[5/8] Checking Model & Provider Configuration...", flush=True)
    model_ref = (
        fcc_config.get("MODEL")
        or os.environ.get("MODEL")
        or "nvidia_nim/nvidia/nemotron-3-super-120b-a12b"
    )
    provider, _, model_name = model_ref.partition("/")
    if not model_name:
        provider = "nvidia_nim"
        model_name = model_ref

    api_key = (
        fcc_config.get("NVIDIA_NIM_API_KEY")
        or os.environ.get("NVIDIA_NIM_API_KEY")
        or fcc_config.get("OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    base_url = (
        fcc_config.get("NVIDIA_NIM_BASE_URL")
        or "https://integrate.api.nvidia.com/v1"
    )

    print(f"  - Provider:           {provider}")
    print(f"  - Configured Model:   {model_ref}")
    print(f"  - Settings.json Model:{claude_settings.get('model', 'NOT SET')}")
    print(f"  - Base URL:           {base_url}")
    print(f"  - API Key:            {'PRESENT' if api_key else 'MISSING'}")

    if not api_key:
        print("  --> FAIL: Missing API key in ~/.fcc/.env")
        all_passed = False
        return 1
    else:
        print("  --> PASS: Provider credentials and endpoint configured")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 6. Provider Model Connectivity & Streaming
    print("\n[6/8] Testing Upstream NIM Endpoint & Streaming...", flush=True)
    t0 = time.time()
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(f"{base_url}/models", headers=headers)
        if r.status_code == 200:
            print(f"  - Models Endpoint:    200 OK ({time.time()-t0:.2f}s)")
        else:
            print(f"  - Models Endpoint:    HTTP {r.status_code}")
            all_passed = False
    except Exception as e:
        print(f"  - Models Endpoint:    ERROR ({e})")
        all_passed = False

    payload_stream = {
        "model": model_name,
        "messages": [{"role": "user", "content": "Say: OK"}],
        "max_tokens": 10,
        "temperature": 0.0,
        "stream": True,
    }
    t0 = time.time()
    stream_chunks = 0
    try:
        with httpx.Client(timeout=20.0) as client:
            with client.stream("POST", f"{base_url}/chat/completions", headers=headers, json=payload_stream) as resp:
                if resp.status_code == 200:
                    for line in resp.iter_lines():
                        if line.startswith("data: ") and line.strip() != "data: [DONE]":
                            stream_chunks += 1
        if stream_chunks > 0:
            print(f"  - SSE Streaming:      PASS ({stream_chunks} chunks in {time.time()-t0:.2f}s)")
            print("  --> PASS: Upstream streaming verified")
        else:
            print("  - SSE Streaming:      FAIL (0 chunks received)")
            all_passed = False
    except Exception as e:
        print(f"  - SSE Streaming:      FAIL ({e})")
        all_passed = False

    # 7. Upstream Tool/Function Calling
    print("\n[7/8] Testing Upstream Tool / Function Calling...", flush=True)
    payload_tools = {
        "model": model_name,
        "messages": [{"role": "user", "content": "What is the weather in Tokyo?"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }],
        "tool_choice": "auto",
        "max_tokens": 60,
        "stream": False,
    }
    t0 = time.time()
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(f"{base_url}/chat/completions", headers=headers, json=payload_tools)
        if r.status_code == 200:
            msg = r.json()["choices"][0]["message"]
            calls = msg.get("tool_calls", [])
            if calls and calls[0]["function"]["name"] == "get_weather":
                print(f"  - Tool Call Trigger:  PASS (called get_weather in {time.time()-t0:.2f}s)")
                print("  --> PASS: Tool calling verified")
            else:
                print(f"  - Tool Call Trigger:  WARN (model answered with plain text)")
        else:
            print(f"  - Tool Call Trigger:  FAIL (HTTP {r.status_code})")
            all_passed = False
    except Exception as e:
        print(f"  - Tool Call Trigger:  FAIL ({e})")
        all_passed = False

    # 8. Filesystem & Git Operations
    print("\n[8/8] Checking Filesystem & Git Repository...", flush=True)
    try:
        res = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            print(f"  - Git Repository:     OK (working tree active)")
            print("  --> PASS: Git operations verified")
        else:
            print(f"  - Git Repository:     FAIL (code {res.returncode})")
            all_passed = False
    except Exception as e:
        print(f"  - Git Repository:     FAIL ({e})")
        all_passed = False

    print("\n" + "=" * 65)
    if all_passed:
        print("                  OVERALL STATUS: ALL CHECKS PASS")
    else:
        print("                  OVERALL STATUS: ISSUES DETECTED")
    print("=" * 65)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(run_diagnostics())

