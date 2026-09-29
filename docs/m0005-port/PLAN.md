# Porting M0005 (AI Execution Platform) features into FCC

Source design: `D:\My Projects\M0005(AI-Platform)\docs` (architecture 00-13, `decisions/ARCHITECTURE_DECISIONS.md` ADR-001..021)
and its TypeScript implementation `D:\My Projects\M0005(AI-Platform)\packages\core\src`. We port **designs and proven
rules**, re-implemented in idiomatic Python for this repo. Core idea: *a model response is not proof of success* —
lock intent, route around rate limits, verify with real evidence, recover within bounds.

## Ground rules for every agent
- Follow `CLAUDE.md` (no `# type: ignore`, no `from __future__ import annotations` in new files, precise types,
  `JsonObject`/`JsonValue` for JSON, top-level imports, tests for everything incl. edge cases, minimal code).
- Only edit the files your wave/task owns (listed in your prompt). Other agents work concurrently.
- `uv run --no-sync ...` for all commands (the running server locks `fcc-server.exe`, so plain `uv run` may fail to sync).
- Run `ruff format`/`ruff check`/`ty check` **only on your files** (whole-repo format rewrites unrelated files).
- Known pre-existing failures to ignore: provider-proxy tests (`tests/config/test_config.py::test_repair_*`,
  `tests/api/test_admin.py::*proxy*`, `tests/cli/test_entrypoints.py::test_server_startup_repairs_invalid_managed_provider_proxy`),
  `tests/contracts/test_provider_catalog_order.py`, `tests/providers/test_provider_runtime.py::test_create_provider_instantiates_each_builtin`,
  `tests/scripts/test_ci_scripts.py::test_ci_ps1_suppression_only_does_not_require_uv`. Add no new failures.
- Do not commit, do not bump the version (main agent does both).

## Package boundaries (tests/contracts/test_import_boundaries.py)
`core` ← everything. `config`→core. `application`→config,core. `providers`→application,config,core.
`cli`→application,config,core. **`workbench`**→application,cli,config,core (new; all M0005 execution features).
`api`→application,config,core (talks to runtime only via Protocols in `api/ports.py`).
`runtime`→everything (composition root; wires workbench into api via ports).

## Shared infrastructure
- `free_claude_code.core.storage.Store(path)`: SQLite (WAL) with `migrate(feature, [sql...])`, `transaction()`,
  `execute(sql, params) -> lastrowid`, `query(sql, params) -> list[Row]`. One DB file per server:
  `config_dir_path() / "fcc.db"` (runtime creates it; libraries receive a `Store` by injection; tests use `tmp_path`).
  Each feature registers its own tables with its own feature name.
- Chat engine: `free_claude_code.cli.managed.interactive` (`InteractiveClaudeSessions` registry, `InteractiveClaudeSession`
  with `send_user_message`, `control`, `subscribe`, `snapshot`; argv builder `build_interactive_claude_argv`).

## Phases → work items
| Phase | Item | Owner wave |
|---|---|---|
| A | Multi-key endpoint pool per provider: per-key health (HEALTHY/DEGRADED/OPEN/HALF_OPEN), rate-limit cooldown (retry-after or 20 s, never opens circuit), quota 1 h, 3 consecutive transport failures → OPEN 30 s → one HALF_OPEN probe, auth_failed → OPEN until reset; bounded failover (4 attempts), stream failover only before first chunk; health persisted | 1 (pool) |
| A | Usage record per attempt (provider, key label, model, tokens, latency, outcome, failover_from, session id) + endpoint p50/p95 | 1 (pool) |
| A | Per-chat budgets: max turns, wall time, output tokens → interrupt + notice | 2 (integration) |
| B | Intent contract compiler (deterministic MUST/MUST_NOT/PRESERVE/scope + optional model extraction that may only add) | 1 (verify) |
| B | Verification gate: project profile detection (never invent commands), checks (typecheck/build/lint/tests, diff_scope, secret_scan, intent conformance, http/browser optional), risk-based levels, gap matrix, evidence package; only the gate marks VERIFIED | 1 (verify) |
| B | Recovery: failure classifier + fingerprints, ladder bounds (3 per fingerprint, 8 total), recovery prompt generation | 1 (verify) |
| B | Checkpoints: base commit / `git stash create` snapshot before a task; scoped revert of the task's files only | 1 (verify) |
| C | Policy presets (restricted/workspace/privileged + builtin rules: `.env*`/keys read → ask, `.git` writes deny, destructive commands ask, `git push` ask) compiled to Claude Code `--settings` permission rules | 1 (policy) |
| C | Credential vault: AES-256-GCM file, master key protected by Windows DPAPI (macOS Keychain / Linux secret-tool), `.env` values `vault:<name>` resolved at load | 1 (policy) |
| C | Tamper-evident audit log (hash chain, append-only) of permission decisions, config/secret changes, verification outcomes | 1 (policy) |
| D | Task graph orchestrator: planner (model → DAG JSON, deterministic single-node fallback), worktree per mutating node, write-scope leases, run nodes as headless Claude sessions, integrate in dependency order, revert out-of-scope files, report conflicts | 1 (orchestrate) |
| D | Verified-only solution memory (candidate → promoted only on VERIFIED, stale by source hash), injected via `--append-system-prompt` | 1 (orchestrate) |
| D | Event log + evidence package export per task | 1 (verify) + 2 |
| — | Wire everything into chat sessions, routes, runtime, admin API | 2 (integration) |
| — | Chat UI: Verified mode toggle, intent contract card, verification panel/gap matrix, revert, budgets, policy preset, Parallel mode + task graph view, endpoint health chip | 3 (chat UI) |
| — | Admin UI: key pool editor + live health, usage dashboard, vault secrets, audit viewer | 3 (admin UI) |
| E | Benchmark suite: seeded scenario repos, time-to-verified-outcome, false-completion = hard failure | 3 (bench) |
| — | Docs: update student guide + feature docs | 3 (docs) |
| — | Full CI, real NIM end-to-end runs, version bump, commit | 4 (main) |
