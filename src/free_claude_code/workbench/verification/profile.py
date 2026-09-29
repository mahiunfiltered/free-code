"""Project profile detection and risk-based verification plans (M0005 doc 08 §2-4).

Evidence-only: a command is reported only when the project declares it (package.json
script, declared tool dependency / [tool.x] table, go.mod, Cargo.toml). Nothing is invented.

    detect_profile(root) -> ProjectProfile   # commands keyed typecheck/build/lint/test
    plan_checks(contract, profile, level=None, http_probes=()) -> VerificationPlan
"""

import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from free_claude_code.workbench.intent import IntentContract

type Level = Literal["minimal", "standard", "deep"]
type CheckKind = Literal[
    "typecheck", "build", "lint", "unit_test", "secret_scan", "diff_scope",
    "intent_conformance", "http_probe",
]  # fmt: skip

LEVELS: tuple[Level, ...] = ("minimal", "standard", "deep")


@dataclass
class ProjectProfile:
    root: str
    ecosystem: str  # node | python | go | rust | none
    commands: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class HttpProbe:
    url: str
    expect_status: int = 200


@dataclass
class PlannedCheck:
    id: str
    kind: CheckKind
    required: bool
    argv: list[str] | None = None
    probe: HttpProbe | None = None


@dataclass
class VerificationPlan:
    level: Level
    checks: list[PlannedCheck]
    reasons: list[str]


def _node(root: Path) -> ProjectProfile | None:
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(pkg, dict):
        return None
    pm = next(
        (name for lock, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun")) if (root / lock).exists()),
        "npm",
    )  # fmt: skip
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    commands = {
        key: [pm, "run", key]
        for key in ("typecheck", "build", "lint", "test")
        if isinstance(scripts.get(key), str)
    }
    if (
        "typecheck" not in commands
        and (root / "tsconfig.json").exists()
        and "typescript" in deps
    ):
        commands["typecheck"] = (
            ["npx", "tsc", "--noEmit"]
            if pm == "npm"
            else [pm, "exec", "tsc", "--noEmit"]
        )
    return ProjectProfile(str(root), "node", commands)


def _python(root: Path) -> ProjectProfile | None:
    pyproject = root / "pyproject.toml"
    reqs = sorted(root.glob("requirements*.txt"))
    if not pyproject.exists() and not reqs and not (root / "setup.cfg").exists():
        return None
    text = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in [pyproject, *reqs]
        if p.exists()
    )
    try:
        tools = (
            tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {})
            if pyproject.exists()
            else {}
        )
    except tomllib.TOMLDecodeError:
        tools = {}

    def declared(name: str) -> bool:
        return (
            name in tools
            or re.search(
                rf"(?<![\w-]){re.escape(name)}(?![\w-])\s*(?:[<>=~!\[;\"',]|$)",
                text,
                re.M,
            )
            is not None
        )

    if (root / "uv.lock").exists():
        prefix = ["uv", "run"]
    elif (root / "poetry.lock").exists() or "poetry" in tools:
        prefix = ["poetry", "run"]
    else:
        venv = (
            root
            / ".venv"
            / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        )
        prefix = [str(venv) if venv.exists() else "python", "-m"]
    commands: dict[str, list[str]] = {}
    if declared("pytest") or (root / "tests" / "conftest.py").exists():
        commands["test"] = [*prefix, "pytest"]
    if declared("ruff"):
        commands["lint"] = [*prefix, "ruff", "check", "."]
    if declared("ty"):
        commands["typecheck"] = [*prefix, "ty", "check"]
    elif declared("mypy"):
        commands["typecheck"] = [*prefix, "mypy", "."]
    return ProjectProfile(str(root), "python", commands)


def _go(root: Path) -> ProjectProfile | None:
    if not (root / "go.mod").exists():
        return None
    return ProjectProfile(
        str(root),
        "go",
        {
            "build": ["go", "build", "./..."],
            "lint": ["go", "vet", "./..."],
            "test": ["go", "test", "./..."],
        },
    )


def _rust(root: Path) -> ProjectProfile | None:
    if not (root / "Cargo.toml").exists():
        return None
    return ProjectProfile(
        str(root), "rust", {"typecheck": ["cargo", "check"], "test": ["cargo", "test"]}
    )


def detect_profile(root: str | Path) -> ProjectProfile:
    """First matching ecosystem wins (node, python, go, rust)."""

    # ponytail: one ecosystem per root; polyglot repos need per-subproject profiles later.
    path = Path(root)
    for detect in (_node, _python, _go, _rust):
        profile = detect(path)
        if profile is not None:
            return profile
    return ProjectProfile(str(path), "none", notes=["no recognised project manifest"])


_RISK_LEVEL: dict[str, Level] = {
    "low": "minimal",
    "medium": "standard",
    "high": "deep",
    "critical": "deep",
}
_COMMAND_KINDS: tuple[tuple[str, CheckKind, Level], ...] = (
    ("typecheck", "typecheck", "minimal"),
    ("test", "unit_test", "minimal"),
    ("build", "build", "standard"),
    ("lint", "lint", "standard"),
)


def plan_checks(
    contract: IntentContract,
    profile: ProjectProfile,
    level: Level | None = None,
    http_probes: tuple[HttpProbe, ...] | list[HttpProbe] = (),
) -> VerificationPlan:
    """Risk-based plan; a floor `level` can only raise the contract's natural level.

    minimal: typecheck, unit_test, secret_scan
    standard: + build, lint (advisory), diff_scope, intent_conformance
    deep: lint becomes required
    MUST_NOT / PRESERVE / scope always force diff_scope + intent_conformance.
    """

    natural = _RISK_LEVEL[contract.risk]
    chosen = max(natural, level or natural, key=LEVELS.index)
    reasons = [f"risk '{contract.risk}' implies {natural}; chosen level {chosen}"]
    rank = LEVELS.index(chosen)
    checks: list[PlannedCheck] = []
    for key, kind, min_level in _COMMAND_KINDS:
        if rank < LEVELS.index(min_level):
            continue
        argv = profile.commands.get(key)
        if argv is None:
            reasons.append(f"{kind}: project declares no {key} command, skipped")
            continue
        checks.append(
            PlannedCheck(
                kind, kind, required=kind != "lint" or chosen == "deep", argv=list(argv)
            )
        )
    checks.append(PlannedCheck("secret_scan", "secret_scan", required=True))
    s = contract.scope
    forced = bool(
        contract.must_not
        or contract.preserve
        or s.allowed_paths
        or s.protected_paths
        or s.prohibited_ops
    )
    if forced or rank >= 1:
        if forced:
            reasons.append(
                "MUST_NOT/PRESERVE/scope present: diff_scope + intent_conformance forced"
            )
        checks.append(PlannedCheck("diff_scope", "diff_scope", required=True))
        checks.append(
            PlannedCheck("intent_conformance", "intent_conformance", required=True)
        )
    checks.extend(
        PlannedCheck(f"http_probe:{i}", "http_probe", required=True, probe=probe)
        for i, probe in enumerate(http_probes, 1)
    )
    return VerificationPlan(chosen, checks, reasons)
