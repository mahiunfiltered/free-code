# Free Claude Code (FCC) — Current Runtime Architecture

## 1. System Topology & Process Hierarchy

```
Desktop Shortcut / Claude-Code.bat
       │
       ▼
launch-claude.ps1 (Windows Terminal / ConPTY launcher)
       │
       ▼
fcc-claude (CLI launcher / managed mode in src/free_claude_code/cli/launchers/claude.py)
       │
       ├──► Stdin / Interactive Input (src/free_claude_code/cli/interactive_input.py)
       │
       ├──► claude.exe (Real Anthropic Claude Code engine)
       │       │
       │       ▼ (HTTP / Anthropic Messages API on 127.0.0.1:8082)
       │
       └──► fcc-server (FastAPI / Uvicorn Proxy Gateway on 127.0.0.1:8082)
               │
               ├──► Core Protocol Translation (Anthropic SSE <-> OpenAI Chat SSE)
               │       ├── src/free_claude_code/core/anthropic/
               │       └── src/free_claude_code/core/openai_responses/
               │
               ├──► Provider Layer (Multi-Provider Dispatch)
               │       ├── NVIDIA NIM (nvidia/nemotron-3-super-120b-a12b)
               │       ├── OpenAI Chat / OpenAI Compatible
               │       ├── OpenAI Codex
               │       ├── DeepSeek, Groq, Kilo, Mistral, Vertex, Gemini
               │       └── Cloudflare, GitHub Models, OpenRouter
               │
               └──► Tool Calling & Streaming State Machines
```

---

## 2. Component Breakdown

### A. Launcher & Shell Layer
* **Launchers**: `Claude-Code.bat`, `launch-claude.ps1`, `src/free_claude_code/cli/launchers/claude.py`, `src/free_claude_code/cli/launchers/common.py`.
* **Subprocess Spawning**: Invokes `claude.exe` located at `C:\Users\yemin\.local\bin\claude.exe` or `npm` global path.
* **Console Mode**: ConPTY / Windows Terminal host with direct VT100 and raw input mode.

### B. Shell & Tool Execution
* **Tools**: `Bash`, `Write`, `Edit`, `MultiEdit`, `Glob`, `Grep`, `LS`.
* **Hooks**: `ruflo-core` (now cross-platform via `ruflo-hook.cjs`), `codebase-memory` (`cbm-session-reminder.js`, `cbm-code-discovery-gate.js`), `caveman` statusline/activate.
* **Permissions**: Workspace-scoped permissions configured in `~/.claude/settings.json`.

### C. Gateway & API Translation
* **Server**: FastAPI application in `src/free_claude_code/api/app.py` listening on `127.0.0.1:8082`.
* **Endpoints**: `/v1/messages` (Anthropic Messages API endpoint used by `claude.exe`), `/v1/models`, `/health`, `/admin`.
* **SSE Emitter**: Translates OpenAI streaming chunks into Anthropic SSE events (`message_start`, `content_block_start`, `content_block_delta`, `message_delta`, `message_stop`).

### D. Provider Architecture
* **Upstream**: Configured via `~/.fcc/.env` and `src/free_claude_code/config/provider_catalog.py`.
* **Active Provider**: NVIDIA NIM with model `nvidia/nemotron-3-super-120b-a12b`.
* **Tool Calling**: Native OpenAI tool calling translated to Anthropic `tool_use` blocks.

---

## 3. Areas Targeted for Autonomous Overhaul

1. **Centralized Windows Shell Resolver (`resolve_shell()`)**: Ensure all sub-modules, hooks, and tools use a unified resolver supporting PowerShell, cmd, Git Bash, and WSL.
2. **Large Stdin Prompt Transport**: Eliminate 32 KB command-line limits by feeding prompts through stdin streams / IPC pipes.
3. **Official OpenAI Provider Integration**: Direct support for `OPENAI_API_KEY` and `OPENAI_BASE_URL` alongside existing providers.
4. **Autonomous Parallel Task Orchestrator**: Multi-agent scheduling with concurrency limits (`MAX_PARALLEL_AGENTS`), role specializations (`PLANNER`, `RESEARCHER`, `CODER`, `REVIEWER`), and file conflict protection.
5. **Smart Retries, Recovery & Anti-Stagnation Watchdog**: 30-second activity watchdog, 3-attempt bounded recovery loop, and fallback model routing.
6. **Diagnostics & Benchmarks**: Live execution progress UI, comprehensive `health-check`, and sequential vs. parallel speedup benchmarks.
