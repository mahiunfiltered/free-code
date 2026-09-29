"""Bounded recovery after a failed verification (M0005 doc 08 §11-13).

    classify(output, check_kind="") -> Category
    decide(evidence, history) -> RecoveryDecision   # history: primary fingerprints of past attempts
    build_recovery_prompt(contract, evidence, attempt, repeated=False) -> str

Ladder bounds: at most 3 attempts per fingerprint and 8 in total, then give_up. security,
scope and intent failures (and NEEDS_REVIEW packages) go straight to ask_user: they need a
human decision, not an automated fix. Each category walks its own strategy ladder, so a
repeated fingerprint always gets a different strategy ("change strategy").
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from free_claude_code.workbench.intent import IntentContract, requirement_rows
from free_claude_code.workbench.verification.failures import (
    Category,
    Failure,
    categorize,
)
from free_claude_code.workbench.verification.gate import EvidencePackage

type Action = Literal["none", "retry", "ask_user", "give_up"]

MAX_PER_FINGERPRINT = 3
MAX_TOTAL = 8
ASK_USER_CATEGORIES: frozenset[Category] = frozenset({"security", "scope", "intent"})

LADDER: dict[Category, tuple[str, ...]] = {
    "environment": ("inspect_environment", "remediate_dependency", "ask_user"),
    "build": ("targeted_fix", "inspect_environment", "alternative_approach"),
    "type": ("targeted_fix", "alternative_approach", "ask_user"),
    "runtime": ("targeted_fix", "reproduce_and_isolate", "alternative_approach"),
    "logic": ("targeted_fix", "reproduce_and_isolate", "alternative_approach"),
    "test": ("targeted_fix", "reproduce_and_isolate", "alternative_approach"),
    "API": ("targeted_fix", "inspect_environment", "ask_user"),
    "database": ("inspect_environment", "targeted_fix", "ask_user"),
    "UI": ("targeted_fix", "alternative_approach", "ask_user"),
    "performance": ("targeted_fix", "alternative_approach", "ask_user"),
    "provider": ("bounded_retry", "bounded_retry", "ask_user"),
    "tool": ("bounded_retry", "alternative_approach", "ask_user"),
}
_STRATEGY_HINT = {
    "targeted_fix": "Fix the root cause shown by the first failure with the smallest possible edit.",
    "reproduce_and_isolate": "Your previous fix did not work. Reproduce the failure, isolate the cause (read the failing code path and test), then fix it.",
    "alternative_approach": "Two attempts failed on the same failure. Discard the previous approach and use a different strategy.",
    "inspect_environment": "Inspect the environment first: check the command exists, dependencies are installed and paths are correct. Do not add new dependencies unless allowed.",
    "remediate_dependency": "The environment is still broken. Repair the missing tool/dependency within the allowed scope, or explain what the user must install.",
    "bounded_retry": "This looks transient. Re-run the failing step once; if it fails again, stop and report.",
}


@dataclass
class RecoveryDecision:
    action: Action
    strategy: str
    reason: str
    fingerprint: str | None = None
    attempt: int = 0  # the attempt number this decision authorises (1-based)
    repeated: bool = False


def classify(output: str, check_kind: str = "") -> Category:
    """Failure category for raw output of a check (or tool) of the given kind."""

    return categorize(check_kind, output)[0]


def _primary(failures: list[Failure]) -> Failure | None:
    for failure in failures:
        if failure.category in ASK_USER_CATEGORIES and not _derived(failure, failures):
            return failure
    return failures[0] if failures else None


def _derived(failure: Failure, failures: list[Failure]) -> bool:
    """A MUST row that failed only because a check failed: fix the check, not the intent."""

    return failure.code == "must_violated" and any(
        f.category not in ASK_USER_CATEGORIES for f in failures
    )


def decide(evidence: EvidencePackage, history: Sequence[str]) -> RecoveryDecision:
    """Next recovery step; append the returned fingerprint to history after each retry."""

    if evidence.disposition == "VERIFIED":
        return RecoveryDecision("none", "none", "verified")
    if evidence.disposition == "NEEDS_REVIEW":
        return RecoveryDecision(
            "ask_user",
            "ask_user",
            "; ".join(evidence.blocking_reasons[:3]) or "needs review",
        )
    primary = _primary(evidence.failures)
    if primary is None:
        return RecoveryDecision(
            "ask_user", "ask_user", "verification failed without a structured failure"
        )
    fp = primary.fingerprint
    if primary.category in ASK_USER_CATEGORIES:
        return RecoveryDecision(
            "ask_user",
            "ask_user",
            f"{primary.category} failure needs a user decision: {primary.message[:200]}",
            fp,
        )
    if len(history) >= MAX_TOTAL:
        return RecoveryDecision(
            "give_up",
            "give_up",
            f"recovery budget exhausted ({MAX_TOTAL} attempts)",
            fp,
        )
    seen = history.count(fp)
    if seen >= MAX_PER_FINGERPRINT:
        return RecoveryDecision(
            "give_up",
            "give_up",
            f"same failure after {seen} attempts: {primary.message[:200]}",
            fp,
        )
    strategy = LADDER.get(
        primary.category, ("targeted_fix", "alternative_approach", "ask_user")
    )[seen]
    if strategy == "ask_user":
        return RecoveryDecision(
            "ask_user",
            strategy,
            f"{primary.category} failure persists after {seen} attempts",
            fp,
        )
    return RecoveryDecision(
        "retry",
        strategy,
        f"{primary.category}: {primary.code}",
        fp,
        len(history) + 1,
        seen > 0,
    )


def _tail(text: str, lines: int = 40, chars: int = 3000) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[-chars:]


def build_recovery_prompt(
    contract: IntentContract,
    evidence: EvidencePackage,
    attempt: int,
    repeated: bool = False,
    strategy: str = "targeted_fix",
) -> str:
    """Concise fix instruction for the coding agent after a failed verification."""

    parts = [
        f"Verification FAILED (recovery attempt {attempt}/{MAX_TOTAL}). Fix only what the evidence below shows."
    ]
    if repeated:
        parts.append(
            "NOTE: this is the SAME failure as a previous attempt - change strategy, do not repeat the previous fix."
        )
    parts.append(_STRATEGY_HINT.get(strategy, _STRATEGY_HINT["targeted_fix"]))
    for check in evidence.checks:
        if check.status not in ("failed", "error"):
            continue
        header = f"\n## {check.id}: {check.status}"
        if check.command:
            header += f" - `{' '.join(check.command)}` exit {check.exit_code}"
        parts.append(header)
        for f in check.failures[:5]:
            loc = (
                f" ({f.path}:{f.line})"
                if f.path and f.line
                else f" ({f.path})"
                if f.path
                else ""
            )
            parts.append(f"- [{f.category}] {f.message.splitlines()[0][:300]}{loc}")
        if check.output:
            parts.append("```\n" + _tail(check.output) + "\n```")
    failed_rows = [r for r in evidence.gap_matrix if r.status == "failed"]
    if failed_rows:
        parts.append("\n## Unmet requirements")
        parts += [
            f"- {r.requirement_id} {r.text}: {'; '.join(r.evidence)[:300]}"
            for r in failed_rows
        ]
    constraints = [
        f"- {rid} {kind.upper()}: {text}"
        for rid, kind, text in requirement_rows(contract)
        if kind in ("must", "must_not", "preserve")
    ]
    if constraints:
        parts += ["\n## Constraints (verbatim, locked)", *constraints]
    s = contract.scope
    if s.allowed_paths or s.protected_paths:
        parts.append(
            f"Scope: only modify {s.allowed_paths or 'anything'}; never modify {s.protected_paths or 'nothing'}."
        )
    parts.append(
        "\nMake the minimal change. Do not weaken, skip or delete tests, and do not edit protected files. "
        "The verification gate will re-run; do not claim success yourself."
    )
    return "\n".join(parts)
