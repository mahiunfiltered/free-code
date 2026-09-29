"""Permission presets compiled to Claude Code ``--settings`` permission rules (ADR-018).

Claude Code evaluates ``deny`` then ``ask`` then ``allow`` and merges rule lists across
settings scopes, so the builtin ask/deny rules emitted here always win over any allow
rule from the user's own settings. Presets can only add restrictions on top of them;
project rules may only add ``ask``/``deny`` (never ``allow``).

Rule syntax follows https://code.claude.com/docs/en/permissions:
``Tool`` or ``Tool(specifier)``; Bash/PowerShell specifiers are command patterns with
``*`` wildcards; Read/Edit specifiers are gitignore paths (``//abs``, ``~/home``,
``/settings-relative``, ``./cwd`` or bare); WebFetch uses ``domain:host``.
Command rules match the command text Claude writes, not every way to spell it
(``/bin/rm``, ``sh -c 'rm'``), so they are guard rails, not a sandbox.
"""

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from free_claude_code.core.json_types import JsonObject

type Decision = Literal["allow", "ask", "deny"]


class Preset(StrEnum):
    RESTRICTED = "restricted"
    WORKSPACE = "workspace"
    PRIVILEGED = "privileged"


class PolicyError(ValueError):
    """Invalid preset, rule syntax, or a project rule that tries to relax policy."""


@dataclass(frozen=True, slots=True)
class Rule:
    decision: Decision
    rule: str
    reason: str

    def __str__(self) -> str:
        return f"{self.decision.upper():5} {self.rule}  - {self.reason}"


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    """What a chat session passes to ``claude --settings <json> --permission-mode <mode>``."""

    preset: Preset
    permission_mode: str
    settings: JsonObject
    rules: tuple[Rule, ...]

    def settings_json(self) -> str:
        return json.dumps(self.settings, separators=(",", ":"))


PROTECTED_BRANCHES = frozenset({"main", "master"})

# ADR-018 / M0005 DESTRUCTIVE_COMMAND_NAMES (Windows + POSIX).
DESTRUCTIVE_COMMANDS = (
    "rm",
    "rmdir",
    "rd",
    "del",
    "erase",
    "format",
    "diskpart",
    "shutdown",
    "reg",
    "takeown",
    "icacls",
    "chown",
    "chmod",
    "mkfs",
    "dd",
    "Remove-Item",
    "Format-Volume",
    "Clear-Disk",
    "Stop-Computer",
    "Restart-Computer",
)
NETWORK_COMMANDS = (
    "curl",
    "wget",
    "Invoke-WebRequest",
    "Invoke-RestMethod",
    "ssh",
    "scp",
)
SECRET_READ_PATTERNS = (".env*", "*.pem", "*.key", "id_rsa*", "id_ed25519*")

_SHELLS = ("Bash", "PowerShell")
_KNOWN_TOOLS = frozenset(
    {
        "Agent",
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "NotebookEdit",
        "PowerShell",
        "Read",
        "WebFetch",
        "WebSearch",
        "Write",
    }
)
_PATH_TOOLS = frozenset({"Read", "Edit"})
_PATHLESS_TOOLS = frozenset(
    {"Write", "NotebookEdit", "Glob"}
)  # path rules never consulted
_RULE_RE = re.compile(r"^(?P<tool>[A-Za-z][A-Za-z0-9_]*)(?:\((?P<spec>.+)\))?$", re.S)


def validate_rule(rule: str) -> None:
    """Raise ``PolicyError`` unless ``rule`` is valid, effective Claude Code rule syntax."""

    match = _RULE_RE.match(rule)
    if match is None:
        raise PolicyError(f"Malformed permission rule: {rule!r}")
    tool, spec = match.group("tool"), match.group("spec")
    if tool.startswith("mcp__"):
        if spec is not None:
            raise PolicyError(f"MCP rules cannot take a specifier: {rule!r}")
        return
    if tool not in _KNOWN_TOOLS:
        raise PolicyError(f"Unknown tool in permission rule: {rule!r}")
    if spec is None:
        return
    if spec != spec.strip() or "\n" in spec:
        raise PolicyError(f"Specifier has stray whitespace: {rule!r}")
    if tool in _PATHLESS_TOOLS:
        raise PolicyError(
            f"{tool} path rules are never consulted; use Edit/Read: {rule!r}"
        )
    if tool in _PATH_TOOLS:
        if "\\" in spec:
            raise PolicyError(f"Use POSIX '/' separators in path rules: {rule!r}")
        if re.match(r"^[A-Za-z]:", spec):
            raise PolicyError(f"Absolute paths must be written '//c/...': {rule!r}")
        return
    if tool == "WebFetch" and not spec.startswith("domain:"):
        raise PolicyError(f"WebFetch rules must use 'domain:<host>': {rule!r}")
    if tool in _SHELLS:
        if spec.startswith("command:"):
            raise PolicyError(f"Shell rules cannot match the command param: {rule!r}")
        if ":*" in spec[:-2]:
            raise PolicyError(f"':*' is only valid at the end of a pattern: {rule!r}")


def _shell(pattern: str, decision: Decision, reason: str) -> list[Rule]:
    return [Rule(decision, f"{shell}({pattern})", reason) for shell in _SHELLS]


def builtin_rules() -> list[Rule]:
    """ADR-018 restrictions applied under every preset."""

    rules = [
        Rule(
            "ask",
            f"Read(//**/{pattern})",
            "reading a likely secret file needs approval",
        )
        for pattern in SECRET_READ_PATTERNS
    ]
    rules.append(Rule("deny", "Edit(//**/.git/**)", "never write inside .git; use git"))
    for name in DESTRUCTIVE_COMMANDS:
        rules += _shell(f"{name} *", "ask", "destructive command needs approval")
    rules += _shell("git push *", "ask", "git push needs approval")
    for pattern in (
        "git push *--force*",
        "git push -f *",
        "git push * -f",
        "git push * -f *",
        "git push * +*",
    ):
        rules += _shell(pattern, "deny", "force push is never allowed")
    return rules


def _branch_rules(branch: str | None) -> list[Rule]:
    # ponytail: branch is sampled once at session start; a mid-session checkout is not tracked.
    if branch is None or not (
        branch in PROTECTED_BRANCHES or branch.startswith("release/")
    ):
        return []
    rules: list[Rule] = []
    for verb in ("commit", "merge", "revert", "rebase"):
        rules += _shell(
            f"git {verb} *", "ask", f"git {verb} on protected branch {branch}"
        )
    return rules


def _preset_rules(preset: Preset) -> tuple[str, list[Rule]]:
    if preset is Preset.RESTRICTED:
        rules = [
            Rule("allow", tool, "read-only tools") for tool in ("Read", "Grep", "Glob")
        ]
        rules += [
            Rule("allow", f"Bash(git {verb} *)", "read-only git")
            for verb in ("status", "diff", "log", "show")
        ]
        rules += [
            Rule("deny", tool, "read-only preset")
            for tool in ("Edit", "Write", "NotebookEdit")
        ]
        for pattern in (
            "git commit *",
            "git reset *",
            "git checkout *",
            "mv *",
            "cp *",
            "mkdir *",
            "touch *",
            "npm install *",
            "pip install *",
            "uv add *",
        ):
            rules += _shell(pattern, "deny", "read-only preset")
        for name in DESTRUCTIVE_COMMANDS:
            rules += _shell(f"{name} *", "deny", "read-only preset")
        for name in NETWORK_COMMANDS:
            rules += _shell(f"{name} *", "deny", "read-only preset: no network")
        # dontAsk auto-denies anything that would prompt, so unknown writes are refused.
        return "dontAsk", rules
    if preset is Preset.WORKSPACE:
        rules = [Rule("allow", "Edit(./**)", "edit files in the project")]
        for name in NETWORK_COMMANDS:
            rules += _shell(f"{name} *", "ask", "network access needs approval")
        return "acceptEdits", rules
    if preset is Preset.PRIVILEGED:
        # Explicit ask rules still prompt and deny rules still block in bypassPermissions.
        return "bypassPermissions", []
    raise PolicyError(f"Unknown preset: {preset!r}")


def compile_policy(
    preset: Preset | str,
    *,
    project_rules: list[Rule] | None = None,
    branch: str | None = None,
) -> CompiledPolicy:
    """Compile a preset + builtin rules + tighten-only project rules into Claude settings."""

    try:
        preset = Preset(preset)
    except ValueError as exc:
        raise PolicyError(f"Unknown preset: {preset!r}") from exc
    mode, preset_rules = _preset_rules(preset)
    extra = project_rules or []
    for rule in extra:
        if rule.decision == "allow":
            raise PolicyError(
                f"Project rules may only tighten policy; rejected allow rule {rule.rule!r}"
            )
    rules: list[Rule] = []
    seen: set[tuple[str, str]] = set()
    for rule in [*builtin_rules(), *_branch_rules(branch), *preset_rules, *extra]:
        validate_rule(rule.rule)
        if (rule.decision, rule.rule) not in seen:
            seen.add((rule.decision, rule.rule))
            rules.append(rule)
    permissions: JsonObject = {
        decision: [rule.rule for rule in rules if rule.decision == decision]
        for decision in ("allow", "ask", "deny")
    }
    permissions["defaultMode"] = mode
    if preset is Preset.RESTRICTED:
        permissions["disableBypassPermissionsMode"] = "disable"
    return CompiledPolicy(preset, mode, {"permissions": permissions}, tuple(rules))


def explain(preset: Preset | str, *, branch: str | None = None) -> list[str]:
    """Human-readable rule list for the UI, most restrictive first."""

    compiled = compile_policy(preset, branch=branch)
    order = {"deny": 0, "ask": 1, "allow": 2}
    lines = [f"MODE  {compiled.permission_mode}"]
    lines += [
        str(rule) for rule in sorted(compiled.rules, key=lambda r: order[r.decision])
    ]
    return lines


PROJECT_POLICY_FILE = Path(".fcc") / "policy.json"


def load_project_rules(project_dir: Path) -> list[Rule]:
    """Read ``<project>/.fcc/policy.json`` ``{"ask": [...], "deny": [...]}``; allow is rejected."""

    path = project_dir / PROJECT_POLICY_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"Cannot read project policy {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"Project policy {path} must be a JSON object")
    if data.get("allow"):
        raise PolicyError(f"Project policy {path} may not contain allow rules")
    unknown = set(data) - {"ask", "deny", "allow"}
    if unknown:
        raise PolicyError(f"Project policy {path} has unknown keys: {sorted(unknown)}")
    rules: list[Rule] = []
    for decision in ("ask", "deny"):
        entries = data.get(decision, [])
        if not isinstance(entries, list) or not all(
            isinstance(e, str) for e in entries
        ):
            raise PolicyError(
                f"Project policy {path}: {decision!r} must be a list of strings"
            )
        for entry in entries:
            validate_rule(entry)
            rules.append(Rule(decision, entry, f"project policy ({path.name})"))
    return rules
