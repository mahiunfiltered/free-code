"""Verification gate (M0005 doc 08, ADR-003): the only producer of VERIFIED.

    run_gate(contract, cwd, checkpoint=None, *, profile=None, level=None, http_probes=(),
             judge=None, command_timeout=600.0, output_cap=64_000) -> EvidencePackage
    compute_disposition(checks, gap_matrix) -> (disposition, blocking_reasons)
    EvidencePackage.to_json() / render_markdown(package_json)

Disposition: FAILED_VERIFICATION if a required check failed/errored or a requirement row
failed; NEEDS_REVIEW if a required check was skipped/undecided, a row lacks evidence, or no
project command actually passed; otherwise VERIFIED. The optional outcome judge (ModelClient)
grades each MUST from evidence summaries: it may fail a row or cover a row that lacked
evidence, but can never turn a failed row or failed command check into a pass.
"""

import asyncio
import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast

from free_claude_code.core.json_types import JsonObject, JsonValue
from free_claude_code.workbench.checkpoints import Checkpoint, TreeDiff, diff_since, git
from free_claude_code.workbench.ignore import diff_ignore_patterns, split_ignored
from free_claude_code.workbench.intent import IntentContract, ModelClient
from free_claude_code.workbench.verification.checks import (
    DEFAULT_OUTPUT_CAP,
    CheckResult,
    GapRow,
    conformance_result,
    diff_scope,
    gap_matrix,
    http_probe,
    run_command_check,
    secret_scan,
)
from free_claude_code.workbench.verification.failures import Failure
from free_claude_code.workbench.verification.profile import (
    HttpProbe,
    Level,
    ProjectProfile,
    detect_profile,
    plan_checks,
)

type Disposition = Literal["VERIFIED", "FAILED_VERIFICATION", "NEEDS_REVIEW"]

_COMMAND_KINDS = ("typecheck", "build", "lint", "unit_test")
EVIDENCE_OUTPUT_CAP = 8_000


@dataclass
class EvidencePackage:
    contract: IntentContract
    level: Level
    disposition: Disposition
    blocking_reasons: list[str]
    checks: list[CheckResult]
    gap_matrix: list[GapRow]
    failures: list[Failure]
    warnings: list[str]
    base_commit: str | None  # HEAD when the task started
    checkpoint_commit: str | None  # snapshot of the working tree when the task started
    head_commit: str | None  # HEAD at verification time
    snapshot_tree: str | None  # working tree at verification time
    diff_sha256: str | None
    diff_stat: str
    changed_files: list[str]
    started_at: str
    completed_at: str
    profile_commands: dict[str, list[str]] = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        return cast(JsonObject, asdict(self))

    def to_markdown(self) -> str:
        return render_markdown(self.to_json())


def _now() -> str:
    return datetime.now(UTC).isoformat()


def compute_disposition(
    checks: list[CheckResult], rows: list[GapRow]
) -> tuple[Disposition, list[str]]:
    failed: list[str] = []
    review: list[str] = []
    for c in checks:
        if not c.required:
            continue
        if c.status in ("failed", "error"):
            failed.append(f"required check '{c.id}' {c.status}: {c.summary}")
        elif c.status in ("skipped", "needs_review"):
            review.append(f"required check '{c.id}' {c.status}: {c.summary}")
    for row in rows:
        if row.status == "failed":
            failed.append(f"requirement {row.requirement_id} failed: {row.text}")
        elif row.status == "missing":
            review.append(
                f"requirement {row.requirement_id} has no evidence: {row.text}"
            )
    if not any(
        c.kind in (*_COMMAND_KINDS, "http_probe") and c.status == "passed"
        for c in checks
    ):
        review.append(
            "no project command or probe passed: nothing executable proves the change"
        )
    if failed:
        return "FAILED_VERIFICATION", failed + review
    if review:
        return "NEEDS_REVIEW", review
    return "VERIFIED", []


_JUDGE_SYSTEM = """You are a strict outcome judge for a software change.
For each MUST requirement decide from the evidence ONLY whether it is fulfilled.
Respond with ONLY JSON: {"verdicts": [{"id": "M1", "verdict": "pass|fail|unknown", "reason": "..."}]}.
Use "unknown" when the evidence does not show it. Never assume."""


async def _apply_judge(
    model: ModelClient,
    rows: list[GapRow],
    checks: list[CheckResult],
    diff_stat: str,
    timeout: float,
) -> list[str]:
    musts = [r for r in rows if r.kind == "must"]
    if not musts:
        return []
    user = json.dumps(
        {
            "requirements": [
                {"id": r.requirement_id, "text": r.text, "status": r.status}
                for r in musts
            ],
            "checks": [
                {"id": c.id, "status": c.status, "summary": c.summary} for c in checks
            ],
            "diff_stat": diff_stat[-3000:],
        }
    )
    try:
        raw = await asyncio.wait_for(model.complete(_JUDGE_SYSTEM, user), timeout)
        start = raw.find("{")
        obj, _ = json.JSONDecoder().raw_decode(raw[start:]) if start >= 0 else (None, 0)
        verdicts = obj.get("verdicts") if isinstance(obj, dict) else None
        if not isinstance(verdicts, list):
            raise ValueError("missing 'verdicts' list")
    except Exception as exc:
        return [f"outcome judge ignored: {exc}"]
    by_id = {r.requirement_id: r for r in musts}
    for v in verdicts:
        if not isinstance(v, dict) or v.get("id") not in by_id:
            continue
        row = by_id[str(v["id"])]
        reason = str(v.get("reason", ""))[:300]
        if v.get("verdict") == "fail":
            row.status = "failed"
            row.evidence.append(f"judge: fail - {reason}")
        elif v.get("verdict") == "pass" and row.status == "missing":
            row.status = "covered"
            row.evidence.append(f"judge: pass - {reason}")
    return []


_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def drop_ignored(diff: TreeDiff, patterns: list[str]) -> tuple[TreeDiff, list[str]]:
    """Remove ignored paths from the files list and the unified diff text."""

    _, ignored = split_ignored([f.path for f in diff.files], patterns)
    if not ignored:
        return diff, []
    skip = set(ignored)
    kept: list[str] = []
    dropping = False
    for line in diff.text.splitlines(keepends=True):
        header = _DIFF_HEADER.match(line.rstrip("\r\n"))
        if header:
            dropping = header.group(2) in skip
        if not dropping:
            kept.append(line)
    filtered = TreeDiff(
        files=[f for f in diff.files if f.path not in skip],
        text="".join(kept),
        stat=diff.stat,
        tree=diff.tree,
    )
    return filtered, ignored


def _head(root: str) -> str | None:
    try:
        return git(root, "rev-parse", "-q", "--verify", "HEAD").strip()
    except subprocess.CalledProcessError:
        return None


def _cap(result: CheckResult, cap: int) -> CheckResult:
    if len(result.output) > cap:
        result.output = "...[truncated]...\n" + result.output[-cap:]
        result.output_truncated = True
    return result


async def run_gate(
    contract: IntentContract,
    cwd: str,
    checkpoint: Checkpoint | None = None,
    *,
    profile: ProjectProfile | None = None,
    level: Level | None = None,
    http_probes: tuple[HttpProbe, ...] | list[HttpProbe] = (),
    judge: ModelClient | None = None,
    command_timeout: float = 600.0,
    output_cap: int = DEFAULT_OUTPUT_CAP,
    judge_timeout: float = 60.0,
) -> EvidencePackage:
    """Run the risk-based plan and return the evidence package (never raises for check failures)."""

    started = _now()
    profile = profile or await asyncio.to_thread(detect_profile, cwd)
    plan = plan_checks(contract, profile, level, http_probes)
    warnings = [*plan.reasons, *profile.notes]
    diff: TreeDiff | None = None
    head_now: str | None = None
    if checkpoint is not None and checkpoint.supported and checkpoint.root:
        try:
            diff = await asyncio.to_thread(diff_since, checkpoint)
            head_now = await asyncio.to_thread(_head, checkpoint.root)
        except (subprocess.CalledProcessError, OSError) as exc:
            warnings.append(f"diff unavailable: {exc}")
        if diff is not None:
            patterns, config_warnings = diff_ignore_patterns(profile.root)
            warnings += config_warnings
            diff, ignored = drop_ignored(diff, patterns)
            if ignored:
                shown = ", ".join(ignored[:10])
                more = f" (+{len(ignored) - 10} more)" if len(ignored) > 10 else ""
                warnings.append(
                    f"ignored {len(ignored)} tool/agent state file(s) in the diff: "
                    f"{shown}{more}"
                )
    else:
        warnings.append("no git checkpoint: diff-based checks cannot run")

    results: list[CheckResult] = []
    for check in plan.checks:
        if check.kind in _COMMAND_KINDS:
            result = await run_command_check(
                check, profile.root, timeout=command_timeout, output_cap=output_cap
            )
            results.append(_cap(result, EVIDENCE_OUTPUT_CAP))
        elif check.kind == "secret_scan":
            results.append(secret_scan(check, diff))
        elif check.kind == "diff_scope":
            results.append(diff_scope(check, contract, diff))
        elif check.kind == "http_probe":
            results.append(await http_probe(check))

    head_moved = None
    if diff is not None and checkpoint is not None:
        head_moved = head_now != checkpoint.head
    rows = gap_matrix(contract, diff, results, head_moved)
    if judge is not None:
        warnings += await _apply_judge(
            judge, rows, results, diff.stat if diff else "", judge_timeout
        )
    results += [
        conformance_result(c, rows)
        for c in plan.checks
        if c.kind == "intent_conformance"
    ]

    disposition, blocking = compute_disposition(results, rows)
    return EvidencePackage(
        contract=contract,
        level=plan.level,
        disposition=disposition,
        blocking_reasons=blocking,
        checks=results,
        gap_matrix=rows,
        failures=[f for r in results for f in r.failures],
        warnings=warnings,
        base_commit=checkpoint.head if checkpoint else None,
        checkpoint_commit=checkpoint.commit if checkpoint else None,
        head_commit=head_now,
        snapshot_tree=diff.tree if diff else None,
        diff_sha256=hashlib.sha256(diff.text.encode()).hexdigest() if diff else None,
        diff_stat=diff.stat if diff else "",
        changed_files=[f.path for f in diff.files] if diff else [],
        started_at=started,
        completed_at=_now(),
        profile_commands=profile.commands,
    )


def _items(value: JsonValue) -> list[JsonObject]:
    return (
        [cast(JsonObject, v) for v in value if isinstance(v, dict)]
        if isinstance(value, list)
        else []
    )


def render_markdown(package: JsonObject) -> str:
    """Human-readable evidence report from EvidencePackage.to_json() output."""

    contract = package.get("contract")
    goal = contract.get("goal", "") if isinstance(contract, dict) else ""
    lines = [
        f"# Evidence package: {package.get('disposition')}",
        "",
        f"- Goal: {goal}",
        f"- Level: {package.get('level')}",
        f"- Base commit: {package.get('base_commit') or 'n/a'}",
        f"- Checkpoint snapshot: {package.get('checkpoint_commit') or 'n/a'}",
        f"- Diff sha256: {package.get('diff_sha256') or 'n/a'}",
        f"- Started: {package.get('started_at')} / completed: {package.get('completed_at')}",
        "",
        "## Checks",
        "",
        "| check | status | exit | duration s | command | summary |",
        "|---|---|---|---|---|---|",
    ]
    for c in _items(package.get("checks")):
        argv = c.get("command")
        command = " ".join(str(a) for a in argv) if isinstance(argv, list) else ""
        summary = str(c.get("summary", "")).replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(
            f"| {c.get('id')} | {c.get('status')} | {c.get('exit_code')} | {c.get('duration_s')} | `{command}` | {summary} |"
        )
    lines += [
        "",
        "## Gap matrix",
        "",
        "| id | kind | requirement | status | evidence |",
        "|---|---|---|---|---|",
    ]
    for r in _items(package.get("gap_matrix")):
        evidence = r.get("evidence")
        ev = "; ".join(str(e) for e in evidence) if isinstance(evidence, list) else ""
        lines.append(
            f"| {r.get('requirement_id')} | {r.get('kind')} | {r.get('text')} | {r.get('status')} | {ev.replace('|', '/')} |"
        )
    for title, key in (
        ("Blocking reasons", "blocking_reasons"),
        ("Warnings", "warnings"),
    ):
        values = package.get(key)
        if isinstance(values, list) and values:
            lines += ["", f"## {title}", "", *(f"- {v}" for v in values)]
    failures = _items(package.get("failures"))
    if failures:
        lines += ["", "## Failures", ""]
        for f in failures:
            loc = f"{f.get('path')}:{f.get('line')}" if f.get("path") else ""
            lines.append(
                f"- [{f.get('category')}] {f.get('check')} {loc} {str(f.get('message', '')).splitlines()[0][:200]}"
            )
    if package.get("diff_stat"):
        lines += [
            "",
            "## Diff stat",
            "",
            "```",
            str(package.get("diff_stat")).rstrip(),
            "```",
        ]
    return "\n".join(lines) + "\n"
