"""Tool/agent-state paths every workbench diff ignores (gate, scope checks, reports).

Hooks and plugins write state into every project during a run (e.g. ruflo's
``.claude-flow/policy/state.json``). Such paths are never verified, reverted,
integrated or reported as out-of-scope; they may be listed once as ignored
tool-state files. Projects add globs in ``.fcc/verify.json`` ``{"ignore": [...]}``.
"""

import json
from pathlib import Path

from free_claude_code.workbench.intent import matches_any

# Not .claude/**: tasks may edit settings.
DEFAULT_DIFF_IGNORE = (
    "**/.claude-flow/**",
    "**/.impeccable/**",
    "**/.fcc-worktrees/**",
    "**/.fcc-bench/**",
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
)
VERIFY_CONFIG_FILE = Path(".fcc") / "verify.json"


def diff_ignore_patterns(project_dir: str | Path) -> tuple[list[str], list[str]]:
    """Default ignore globs plus the project's ``.fcc/verify.json`` ones.

    Returns (patterns, warnings); an unreadable/invalid file is a warning, not an error.
    """

    patterns = list(DEFAULT_DIFF_IGNORE)
    path = Path(project_dir) / VERIFY_CONFIG_FILE
    if not path.is_file():
        return patterns, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return patterns, [f"{VERIFY_CONFIG_FILE.as_posix()} ignored: {exc}"]
    extra = data.get("ignore", []) if isinstance(data, dict) else None
    if not isinstance(extra, list) or not all(isinstance(p, str) for p in extra):
        return patterns, [
            f'{VERIFY_CONFIG_FILE.as_posix()} ignored: "ignore" must be a list of globs'
        ]
    return patterns + [p for p in extra if p.strip()], []


def split_ignored(paths: list[str], patterns: list[str]) -> tuple[list[str], list[str]]:
    """``(kept, ignored)`` partition of repo-relative ``paths``, order preserved."""

    kept: list[str] = []
    ignored: list[str] = []
    for path in paths:
        (ignored if matches_any(path, patterns) else kept).append(path)
    return kept, ignored
